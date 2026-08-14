#!/usr/bin/env python3
"""步骤2：读取当日去重信号，调用 AI 生成结构化日报 JSON。

高延迟优化（v3.0）：
- 移除降级兜底：AI 不可用则直接失败， workflow 随之失败，不会发布非 AI 内容。
- 支持 AI_BASE_URL_POOL（多镜像/端点 fallback）与 AI_FORCE_NON_STREAM（强制非流式），
  并允许 AI_REQUEST_TIMEOUT 环境变量覆盖超时，以适配国内镜像或海外 Runner 慢链路。
- 保留 DNS-over-HTTPS 兜底与代理池，用于海外 Runner 偶发解析抖动。
"""
import os
import sys
import json
import time
import re
import random
import socket
import logging
import datetime
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

SKILL_FILE = C.SKILL_FILE
DATA_DIR = C.DATA_DIR

RETRY_PER_ENDPOINT = 2      # 每端点重试：收敛防时间爆炸；冗余靠多镜像+DoH+代理池提供
BACKOFF_BASE = 8            # 退避基数 8s：网关高延迟，更长退避让其喘气（用户允许稍晚生成）
REQ_TIMEOUT = (20, 300)     # 默认读超时 300s；可被环境变量 AI_REQUEST_TIMEOUT 覆盖
GEN_RETRIES = 4             # 外层生成重试 4 次：用户要求必须 AI 输出、可稍晚，多给几次机会
MAX_INPUT_SIGNALS = 50      # 送入 AI 的候选上限（控制上下文长度保稳定）
MAX_OUTPUT_TOKENS = 6000    # 输出 token 上限（收敛输出，降低超时/截断概率）

# —— 分批筛选（避免大量候选被直接截断丢弃，提升内容丰富度）——
SCREEN_THRESHOLD = 60     # 候选超过此数才启用分批筛选；否则全量直送生成（省调用、保速度）
SCREEN_BATCH = 60         # 每批送入筛选的候选数（平衡单批覆盖度与调用次数）
SCREEN_KEEP_PER_BATCH = 10  # 每批最多保留的精华条数（仅作日志参考，实际取汇总 TopN）
SCREEN_FINAL_CAP = 35     # 筛选后送入最终生成的最大条数（收敛上下文，减少 400/截断）


def _req_timeout():
    """允许通过 AI_REQUEST_TIMEOUT 覆盖默认读超时（秒），方便切换国内镜像时调小。"""
    raw = os.environ.get("AI_REQUEST_TIMEOUT", "").strip()
    if raw:
        try:
            return (20, int(raw))
        except ValueError:
            pass
    return REQ_TIMEOUT


def _force_non_stream():
    """是否强制所有 AI 调用走非流式。

    默认强制非流式：GitHub Actions（海外 Runner）→ 国内网关的流式 SSE
    容易被国际链路截断/超时，整包返回更稳定。用户可设置 AI_FORCE_NON_STREAM=0 关闭。
    """
    raw = os.environ.get("AI_FORCE_NON_STREAM", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _trim_signals_for_prompt(signals, max_content=400):
    """为 prompt 裁剪信号：保留完整元数据，content 截断到 max_content 字符，
    减少网关上下文压力，同时保留关键信息供 AI 判断。"""
    out = []
    for s in signals:
        c = dict(s)
        content = c.get("content") or ""
        if len(content) > max_content:
            c["content"] = content[:max_content].rstrip() + "…（已截断）"
        out.append(c)
    return out


def _is_valid_proxy(p):
    """校验代理 URL 合法且非占位符（避免中文/字面占位符被当成真实代理）。"""
    try:
        u = urlparse(p)
    except Exception:
        return False
    if u.scheme not in ("http", "https", "socks5", "socks4"):
        return False
    host = u.hostname or ""
    if not host:
        return False
    if not host.isascii():
        return False
    low = p.lower()
    if any(k in low for k in ("代理", "placeholder", "example", "xxxx",
                              "your_", "ip:端口", "ip:port", "ip:端口")):
        return False
    return True


def _parse_proxies():
    """从 AI_PROXY_POOL 解析代理列表，过滤空值与无效/占位符配置。"""
    raw = os.environ.get("AI_PROXY_POOL", "").strip()
    if not raw:
        return []
    out = []
    for p in re.split(r"[;,\n]", raw):
        p = p.strip()
        if not p:
            continue
        if _is_valid_proxy(p):
            out.append(p)
        else:
            LOG.warning("跳过无效/占位符代理配置: %s", p[:50])
    return out


def _parse_base_urls():
    """解析候选 base_url：显式 AI_BASE_URL 优先，再拼 AI_BASE_URL_POOL（分号/逗号/换行分隔）。
    去重保序，空值/非 URL 占位符过滤。用于 AI 镜像/备用端点 fallback。"""
    primary = os.environ.get("AI_BASE_URL", "https://ai.jinbufenzi.com/v1").strip()
    pool_raw = os.environ.get("AI_BASE_URL_POOL", "").strip()
    seen, out = set(), []
    for u in [primary] + re.split(r"[;,\n]", pool_raw):
        u = u.strip().rstrip("/")
        if not u:
            continue
        low = u.lower()
        if any(k in low for k in ("placeholder", "example", "xxxx", "your_", "example.com")):
            continue
        if not u.startswith(("http://", "https://")):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out or ["https://ai.jinbufenzi.com/v1"]


def _candidate_endpoints():
    """返回候选端点列表：直连(None) 排前，代理随机打散实现轮换。"""
    proxies = _parse_proxies()
    cands = [None] + proxies          # 直连优先；直连失败（DNS 抖动/地理封锁）再尝试代理
    random.shuffle(cands)
    return cands


def _is_retryable_http(status):
    """连接层失败(status=0，无响应体)/限流 429 / 5xx 服务端错误可重试；
    4xx 其他（鉴权/参数错误）直接放弃。"""
    return status == 0 or status == 429 or (500 <= status < 600)


def _retry_after_seconds(resp):
    """解析 Retry-After（整数秒或 HTTP-date），封顶 60s；无效返回 0。
    用于尊重网关/服务端明示的限流冷却时间，避免盲目重试打满配额。"""
    if not resp or not getattr(resp, "headers", None):
        return 0
    ra = resp.headers.get("Retry-After")
    if not ra:
        return 0
    ra = ra.strip()
    try:
        secs = int(ra)
        if secs < 0:
            return 0
        return min(secs, 60)
    except ValueError:
        pass
    # HTTP-date 形式（如 "Wed, 14 Aug 2026 12:00:00 GMT"）
    try:
        import email.utils as eu
        dt = eu.parsedate_to_datetime(ra)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            delta = (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if delta > 0:
                return min(int(delta), 60)
    except Exception:
        pass
    return 0


# ── DNS-over-HTTPS 兜底：海外 Runner 偶发解析不到国内域名时，
#    用 Cloudflare DoH（全球可达）拿到真实 A 记录并固定给目标 host，
#    从而直连也能稳定解析（SNI/证书均保持原 host，验证不受影响）。──
_DNS_PATCH_HOSTS = {}


def _doh_resolve(host):
    """通过 DoH 解析 A 记录，返回 IP 列表。多 provider 轮换，任一成功即可（提升抖动容错）。"""
    import urllib.request
    providers = [
        ("Google",     "https://dns.google/resolve?name=%s&type=A" % host, {"Accept": "application/dns-json"}),
        ("Cloudflare", "https://cloudflare-dns.com/dns-query?name=%s&type=A" % host, {"Accept": "application/dns-json"}),
        ("1.1.1.1",    "https://1.1.1.1/dns-query?name=%s&type=A" % host, {"Accept": "application/dns-json"}),
    ]
    ips = []
    for name, url, hdr in providers:
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            found = [a["data"] for a in d.get("Answer", []) if a.get("type") == 1]
            if found:
                LOG.info("DoH(%s) 解析 %s -> %s", name, host, found)
                ips.extend(found)
        except Exception as e:
            LOG.warning("DoH(%s) 解析 %s 失败: %s", name, host, e)
    # 去重保序
    seen, uniq = set(), []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            uniq.append(ip)
    return uniq


def _install_dns_patch(host):
    """若尚未修补，用 DoH 结果固定该 host 的 getaddrinfo 解析。"""
    if _DNS_PATCH_HOSTS.get(host):
        return
    ips = _doh_resolve(host)
    if not ips:
        return
    target = ips[0]
    orig = socket.getaddrinfo

    def _patched(h, *args, **kwargs):
        if h == host:
            port = args[0] if args and isinstance(args[0], int) else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (target, port))]
        return orig(h, *args, **kwargs)

    socket.getaddrinfo = _patched
    _DNS_PATCH_HOSTS[host] = target
    LOG.info("DoH 兜底：%s 固定解析到 %s（后续直连不再依赖 Runner 本地 DNS）", host, target)


def _call_ai(base_urls, api_key, model, system_prompt, user_prompt, stream=True):
    """调用 /chat/completions，支持多 base_url fallback + 端点轮换 + 重试。
    返回模型原始 content 字符串。

    - base_urls 可为字符串或列表；列表按顺序依次尝试（主镜像 → 备用镜像）。
    - AI_FORCE_NON_STREAM=1 时强制全部调用走非流式，避免国际链路流式分块被截断。
    - AI_REQUEST_TIMEOUT 可覆盖默认读超时。
    """
    if isinstance(base_urls, str):
        base_urls = [base_urls]
    if _force_non_stream():
        stream = False

    start = time.time()
    cands = _candidate_endpoints()
    mode = "stream" if stream else "non-stream"
    last_err = None
    timeout = _req_timeout()

    for base_url in base_urls:
        url = base_url.rstrip("/") + "/chat/completions"
        # 解析目标 host，用 DoH 兜底修补海外 Runner 偶发 DNS 失败
        try:
            _host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(base_url).netloc or "ai.jinbufenzi.com"
        except Exception:
            _host = "ai.jinbufenzi.com"
        _install_dns_patch(_host)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "temperature": 0.5,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "stream": stream,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        for ci, proxy in enumerate(cands):
            proxies = {"http": proxy, "https": proxy} if proxy else None
            label = f"{base_url} | {proxy or '直连'}"
            for attempt in range(1, RETRY_PER_ENDPOINT + 1):
                try:
                    LOG.info("AI 请求 [base=%s 端点 %d/%d=%s] 第 %d/%d 次尝试 (%s)",
                             base_url, ci + 1, len(cands), proxy or "直连",
                             attempt, RETRY_PER_ENDPOINT, mode)
                    resp = requests.post(
                        url, headers=headers, json=payload,
                        proxies=proxies, timeout=timeout, stream=stream,
                    )
                    resp.raise_for_status()

                    if not stream:
                        # 非流式：直接取整包内容
                        msg = (resp.json().get("choices", [{}])[0]
                               .get("message", {}).get("content", ""))
                        if msg and _extract_json(msg) is not None:
                            LOG.info("AI 请求成功（base=%s，端点=%s，模式=non-stream，耗时 %.1fs，约 %d 字）",
                                     base_url, proxy or "直连", time.time() - start, len(msg))
                            return msg
                        LOG.warning("非流式返回无法解析为 JSON，长度 %d", len(msg))
                        raise RuntimeError("non-stream response not valid JSON")

                    # 流式聚合 SSE（强制 UTF-8 解码，避免中文被误判为 Latin-1 破坏 JSON）
                    content = ""
                    raw_dump = []
                    for raw_line in resp.iter_lines(decode_unicode=False):
                        if not raw_line:
                            continue
                        try:
                            line = raw_line.decode("utf-8").strip()
                        except Exception:
                            line = raw_line.decode("utf-8", "replace").strip()
                        raw_dump.append(line)
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if not choices:
                            # 流式结束标记 / usage 统计 chunk / 网关偶发空 chunk：跳过，不崩溃
                            continue
                        delta = (choices[0].get("delta") or {}).get("content")
                        if delta:
                            content += delta
                    if not content:
                        # 诊断：把原始响应前若干行打出来，便于判断是鉴权错误/Challenge/格式差异
                        snippet = " | ".join(raw_dump[:15])[:800]
                        LOG.error("流式响应为空，原始响应片段：%s", snippet)
                        raise RuntimeError("流式响应为空（见上方原始片段）")
                    # 校验 JSON 完整性：流式偶发被网关/CF 截断会导致内容不完整，
                    # 需当作本次尝试失败并退避重试，而非直接返回残缺内容
                    if _extract_json(content) is None:
                        wait = BACKOFF_BASE * (2 ** (attempt - 1))
                        LOG.warning("AI 返回无法解析为 JSON（可能流式被截断），本次尝试失败，%ds 后重试",
                                    wait)
                        time.sleep(wait)
                        continue
                    LOG.info("AI 请求成功（base=%s，端点=%s，模式=stream，耗时 %.1fs，约 %d 字）",
                             base_url, proxy or "直连", time.time() - start, len(content))
                    return content
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.InvalidURL,
                        socket.gaierror) as e:
                    # DNS 解析失败 / 连接被重置 / 超时 —— 典型海外 Runner 抖动或网关高延迟。
                    last_err = e
                    wait = BACKOFF_BASE * (2 ** (attempt - 1))
                    LOG.warning("base=%s 端点 %s 连接失败(%s)，%ds 后重试",
                                base_url, proxy or "直连", type(e).__name__, wait)
                    time.sleep(wait)
                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response else 0
                    body = ""
                    try:
                        if e.response is not None:
                            body = e.response.text[:500]
                    except Exception:
                        pass
                    last_err = e
                    if _is_retryable_http(status):
                        # 尊重服务端 Retry-After（限流冷却），否则用指数退避
                        wait = _retry_after_seconds(e.response) or (BACKOFF_BASE * (2 ** (attempt - 1)))
                        LOG.warning("AI 接口返回 %s（base=%s），%ds 后重试", status, base_url, wait)
                        time.sleep(wait)
                    else:
                        LOG.error("AI 接口返回 %s，中止重试：%s | %s",
                                  status, body, str(e))
                        raise
                except (KeyError, IndexError, json.JSONDecodeError) as e:
                    last_err = e
                    LOG.error("AI 返回结构异常：%s", e)
                    raise
    raise RuntimeError(
        f"所有候选 base_url（{len(base_urls)}）与端点均失败，总耗时 %.1fs，最后错误：{last_err}" % (
            time.time() - start,))


def _extract_json(text):
    """从模型输出里抠出 JSON（兼容 ```json 围栏/前后文字/中文引号/字符串内含括号）。"""
    if not text:
        return None
    s = text.strip()
    if "```" in s:
        s = re.sub(r"```(?:json)?\s*", "", s)
        s = s.replace("```", "")
    start = s.find("{")
    if start == -1:
        return None
    # 括号平衡提取最外层对象（正确处理字符串值内的 { } 与转义字符）
    depth = 0
    in_str = False
    esc = False
    end = -1
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    s = s[start:end + 1]
    for cand in (s,
                 s.replace("“", "\"").replace("”", "\""),
                 s.replace("‘", "'").replace("’", "'")):
        try:
            return json.loads(cand)
        except Exception:
            continue
    # 兜底：去 trailing comma 与 // 注释再试
    try:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", s)
        cleaned = re.sub(r"//[^\n]*", "", cleaned)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)
        return json.loads(cleaned)
    except Exception:
        return None


def _validate(report):
    assert isinstance(report, dict), "顶层不是对象"
    mods = report.get("modules", {})
    for key in ("project_opportunities", "growth_operations", "views_insights"):
        assert key in mods, f"缺少模块 {key}"
        assert isinstance(mods[key], list), f"{key} 不是数组"
    assert "daily_summary" in report, "缺少 daily_summary"
    ds = report["daily_summary"]
    assert isinstance(ds, dict), "daily_summary 不是对象"
    # 容错：AI 偶发漏字段不致命，补默认值即可，避免整跑崩溃
    ds.setdefault("methodology", "")
    ds.setdefault("evidence", [])
    return True


def _empty_report(date):
    return {
        "date": date,
        "timezone": "Asia/Shanghai",
        "modules": {
            "project_opportunities": [],
            "growth_operations": [],
            "views_insights": [],
        },
        "daily_summary": {"methodology": "今日无新增信号", "evidence": []},
    }


def _screen_system_prompt():
    return (
        "你是副业/创业领域的内容筛选专家。任务：从一批候选信号中，"
        "挑出与「副业赚钱、独立开发、创业增长、产品运营、AI 变现、效率工具」"
        "最相关、信息密度最高、最值得写进今日日报的条目。"
        "严格宁缺毋滥：广告、纯资讯快讯、低质或重复内容可跳过。"
        "只输出 JSON：{\"picks\":[{\"id\":\"与输入完全一致的原始 id\","
        "\"score\":1-10,\"reason\":\"一句话理由\"}]}。"
    )


def _screen_one_batch(batch, base_urls, api_key, model, system_prompt):
    user = (
        f"以下是 {len(batch)} 条候选信号（JSON 数组，每条含 id/source_name/title/content/published_at）：\n"
        f"```json\n{json.dumps(batch, ensure_ascii=False, indent=2)}\n```\n"
        "请按规则筛选，仅返回 picks JSON。"
    )
    content = _call_ai(base_urls, api_key, model, system_prompt, user, stream=True)
    data = _extract_json(content)
    return (data or {}).get("picks") or []


def screen_signals(signals, base_urls, api_key, model, system_prompt):
    """分批筛选：打乱后分桶，每桶让 AI 挑精华，汇总按分数取 TopN。
    保证每个内容源都有机会被看到，解决『候选被截断丢弃』导致的丰富度下降。"""
    import random
    random.shuffle(signals)  # 打乱，使每批源混合，避免整批同源于是漏选
    batches = [signals[i:i + SCREEN_BATCH] for i in range(0, len(signals), SCREEN_BATCH)]
    by_id = {s["id"]: s for s in signals if s.get("id")}
    scored = {}
    t0 = time.time()
    for bi, batch in enumerate(batches):
        bs = time.time()
        try:
            picks = _screen_one_batch(batch, base_urls, api_key, model, system_prompt)
        except Exception as e:
            LOG.warning("分批筛选第 %d/%d 批失败，跳过该批：%s", bi + 1, len(batches), e)
            continue
        LOG.info("分批筛选第 %d/%d 批完成，本批耗时 %.1fs，返回 %d 条",
                 bi + 1, len(batches), time.time() - bs, len(picks))
        for p in picks:
            pid = p.get("id")
            if pid not in by_id:
                continue
            try:
                sc = int(float(p.get("score", 0) or 0))
            except Exception:
                sc = 0
            prev = scored.get(pid)
            if prev is None or sc > prev[0]:
                scored[pid] = (sc, p.get("reason", ""))
    ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)
    kept = [by_id[pid] for pid, _ in ranked[:SCREEN_FINAL_CAP]]
    LOG.info("分批筛选完成：%d 候选 → %d 批 → 汇总 %d 条精华 → 取前 %d 条进生成，总耗时 %.1fs",
             len(signals), len(batches), len(scored), len(kept), time.time() - t0)
    return kept


def main():
    overall_t0 = time.time()
    C.ensure_dirs()
    today = C.date_str()
    LOG.info("开始生成 %s 日报", today)

    # AI 配置与候选端点
    base_urls = _parse_base_urls()
    api_key = os.environ.get("AI_API_KEY", "")
    model = os.environ.get("AI_MODEL", "auto")
    if _force_non_stream():
        LOG.info("强制非流式模式：所有 AI 调用使用整包返回（可在 Secret AI_FORCE_NON_STREAM=0 关闭）")

    # 为所有候选 base_url 预做 DNS 补丁（避免首次调用时才解析）
    for u in base_urls:
        try:
            _h = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(u).netloc or "ai.jinbufenzi.com"
        except Exception:
            _h = "ai.jinbufenzi.com"
        _install_dns_patch(_h)

    # 1) 读取信号
    cand_path = os.path.join(DATA_DIR, f"candidates-{today}.json")
    candidates = C.load_json(cand_path, {})
    if isinstance(candidates, list):
        signals = candidates
    else:
        signals = candidates.get("candidates") or candidates.get("items") or []

    if not signals:
        LOG.info("今日无候选信号，写空日报并结束。")
        C.save_json(os.path.join(DATA_DIR, f"report-{today}.json"), _empty_report(today))
        return

    if not api_key:
        LOG.error("缺少 AI_API_KEY（Secret AI_SIDEHUSTLE_API_KEY 未注入），无法生成。")
        raise SystemExit("missing AI key")

    # 2) 候选编排：候选过多 → 分批筛选（AI 分桶挑精华，杜绝截断丢弃）；
    #    中小批量 → 均衡采样保证每个源都有代表；最后统一安全上限。
    if len(signals) > SCREEN_THRESHOLD:
        signals = screen_signals(signals, base_urls, api_key, model, _screen_system_prompt())
    elif len(signals) > MAX_INPUT_SIGNALS:
        from collections import OrderedDict
        groups = OrderedDict()
        for s in signals:
            groups.setdefault(s.get("source_name", "未知"), []).append(s)
        n_src = len(groups) or 1
        per = max(1, -(-MAX_INPUT_SIGNALS // n_src))
        picked, leftover = [], []
        for nm, items in groups.items():
            if len(items) > per:
                picked.extend(items[:per]); leftover.extend(items[per:])
            else:
                picked.extend(items)
        if len(picked) < MAX_INPUT_SIGNALS and leftover:
            leftover.sort(key=lambda x: x.get("source_name", ""))
            picked.extend(leftover[: MAX_INPUT_SIGNALS - len(picked)])
        signals = picked[:MAX_INPUT_SIGNALS]
        LOG.info("候选 %d 条，按 %d 个源均衡采样至 %d 条", len(signals), n_src, len(signals))
    if len(signals) > SCREEN_FINAL_CAP:
        signals = signals[:SCREEN_FINAL_CAP]
    LOG.info("最终送入生成：%d 条候选信号", len(signals))

    # 3) 读取 prompt
    try:
        system_prompt = open(SKILL_FILE, "r", encoding="utf-8").read()
    except Exception as e:
        LOG.error("无法读取 SKILL.md：%s", e)
        raise

    # 为最终生成裁剪信号：保留元数据，content 截断到 400 字符，
    # 减少高延迟网关上下文压力，降低 400/截断概率。
    prompt_signals = _trim_signals_for_prompt(signals, max_content=400)
    user_prompt = (
        f"今天是 {today}（北京时间）。以下是已完成去重的当日增量信号（JSON）：\n"
        f"```json\n{json.dumps(prompt_signals, ensure_ascii=False, indent=2)}\n```\n"
        f"请按 SKILL v2 规则，输出 ai-sidehustle-report 日报 JSON。"
    )

    # 4) 调用 AI（外层生成级重试：应对空生成 / 解析失败 / 结构不完整）
    #    AI 失败直接抛 SystemExit → workflow 失败，不发布非 AI 内容。
    #    最终日报生成使用 non-stream：大输出场景下整包返回更稳定，避免流式截断。
    content = None
    for gen in range(1, GEN_RETRIES + 1):
        try:
            content = _call_ai(base_urls, api_key, model, system_prompt, user_prompt, stream=False)
        except Exception as e:
            LOG.error("AI 调用失败（第 %d/%d 次生成，已用 %.1fs）：%s",
                      gen, GEN_RETRIES, time.time() - overall_t0, e)
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE)
                continue
            raise SystemExit("AI call failed after retries")
        report = _extract_json(content)
        if report is None:
            LOG.error("AI 返回无法解析为 JSON（第 %d/%d 次生成），完整内容(%d字)：\n%s",
                      gen, GEN_RETRIES, len(content), content[:1500])
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE)
                continue
            raise SystemExit("invalid AI json")
        try:
            _validate(report)
        except AssertionError as e:
            LOG.error("结构校验失败（第 %d/%d 次生成）：%s", gen, GEN_RETRIES, e)
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE)
                continue
            raise SystemExit("invalid report structure")
        total = sum(len(report.get("modules", {}).get(k, [])) for k in
                    ("project_opportunities", "growth_operations", "views_insights"))
        if total == 0 and len(signals) > 0:
            LOG.warning("AI 返回 0 条但候选信号有 %d 条（疑似空生成），重生成（%d/%d）",
                        len(signals), gen, GEN_RETRIES)
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE)
                continue
            LOG.warning("已达最大生成次数仍为 0 条，接受空日报。")
        break

    report["date"] = today
    report["timezone"] = "Asia/Shanghai"

    out_path = os.path.join(DATA_DIR, f"report-{today}.json")
    C.save_json(out_path, report)
    total = sum(len(report["modules"].get(k, [])) for k in
                ("project_opportunities", "growth_operations", "views_insights"))
    LOG.info("日报已生成：3 个模块共 %d 条，写入 %s，总耗时 %.1fs",
             total, out_path, time.time() - overall_t0)


if __name__ == "__main__":
    main()
