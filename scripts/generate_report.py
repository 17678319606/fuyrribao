#!/usr/bin/env python3
"""步骤2：读取当日去重信号，调用 AI 生成结构化日报 JSON。

健壮性增强（v2.1）：
- 自动重试：针对 GitHub 海外 Runner 偶发 DNS / 连接失败做指数退避重试，
  覆盖 NameResolutionError / ConnectTimeout / Connection reset 等抖动。
- 可选代理池：通过 Secret `AI_PROXY_POOL` 配置多个国内出口代理
  （逗号 / 分号 / 换行分隔，支持 http / https / socks5）。
  每次运行随机打散实现轮换，并对各端点做探活；
  直连优先，代理作为兜底——既能扛偶发 DNS 抖动，也能解决国内域名被海外解析不到的问题。
"""
import os
import sys
import json
import time
import re
import random
import socket
import logging
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

SKILL_FILE = C.SKILL_FILE
DATA_DIR = C.DATA_DIR

RETRY_PER_ENDPOINT = 4      # 每个候选端点（直连 / 代理）的最大尝试次数
BACKOFF_BASE = 3            # 退避基数（秒），呈指数增长：3 / 6 / 12 / 24s
REQ_TIMEOUT = (15, 180)     # (connect, read)；read 180s 足够容纳流式长生成，且不超过 job 超时
GEN_RETRIES = 3             # main() 外层生成级重试：应对空生成 / 解析失败 / 结构不完整
MAX_INPUT_SIGNALS = 80      # 送入 AI 的候选上限（控制输入上下文，给源站减负）
MAX_OUTPUT_TOKENS = 9000    # 输出 token 上限（兼顾内容量与源站生成耗时，避免 Cloudflare 524）


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


def _candidate_endpoints():
    """返回候选端点列表：直连(None) 排前，代理随机打散实现轮换。"""
    proxies = _parse_proxies()
    cands = [None] + proxies          # 直连优先；直连失败（DNS 抖动/地理封锁）再尝试代理
    random.shuffle(cands)
    return cands


def _is_retryable_http(status):
    """429 限流 / 5xx 服务端错误可重试；4xx 其他（鉴权/参数错误）直接放弃。"""
    return status == 429 or (500 <= status < 600)


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


def _call_ai(base_url, api_key, model, system_prompt, user_prompt):
    """调用 /chat/completions，带端点轮换 + 重试。返回模型原始 content 字符串。"""
    url = base_url.rstrip("/") + "/chat/completions"
    # 解析目标 host（从 base_url 提取），用 DoH 兜底修补海外 Runner 的偶发 DNS 失败
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
        "stream": True,   # 流式：token 持续输出可避免 Cloudflare 524（源站静默超时）
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    cands = _candidate_endpoints()
    last_err = None
    for ci, proxy in enumerate(cands):
        proxies = {"http": proxy, "https": proxy} if proxy else None
        label = proxy or "直连"
        for attempt in range(1, RETRY_PER_ENDPOINT + 1):
            try:
                LOG.info("AI 请求 [端点 %d/%d=%s] 第 %d/%d 次尝试 (stream)",
                         ci + 1, len(cands), label, attempt, RETRY_PER_ENDPOINT)
                resp = requests.post(
                    url, headers=headers, json=payload,
                    proxies=proxies, timeout=REQ_TIMEOUT, stream=True,
                )
                resp.raise_for_status()
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
                LOG.info("AI 请求成功（端点=%s，约 %d 字）", label, len(content))
                return content
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.InvalidURL,
                    socket.gaierror) as e:
                # DNS 解析失败 / 连接被重置 / 超时 —— 典型海外 Runner 抖动
                last_err = e
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                LOG.warning("端点 %s 连接失败(%s)，%ds 后重试",
                            label, type(e).__name__, wait)
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response else 0
                last_err = e
                if _is_retryable_http(status):
                    wait = BACKOFF_BASE * (2 ** (attempt - 1))
                    LOG.warning("AI 接口返回 %s，%ds 后重试", status, wait)
                    time.sleep(wait)
                else:
                    LOG.error("AI 接口返回 %s，中止重试：%s",
                              status, (e.response.text[:300] if e.response else ""))
                    raise
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                last_err = e
                LOG.error("AI 返回结构异常：%s", e)
                raise
    # ── 兜底：非流式请求 ──
    # 流式偶发被网关/CF 中途掐断且多端点重试仍失败时，尝试一次非流式（整包返回，规避分块截断）
    # 风险：生成耗时可能触发 Cloudflare 524；故仅作最后兜底，且用较短 read 超时快速失败
    try:
        LOG.info("流式多端点重试均失败，尝试非流式兜底请求（整包返回）")
        nb_payload = dict(payload)
        nb_payload["stream"] = False
        r2 = requests.post(url, headers=headers, json=nb_payload,
                           proxies=None, timeout=(15, 160))
        r2.raise_for_status()
        msg = (r2.json().get("choices", [{}])[0]
               .get("message", {}).get("content", ""))
        if msg and _extract_json(msg) is not None:
            LOG.info("非流式兜底成功（约 %d 字）", len(msg))
            return msg
        LOG.warning("非流式兜底返回内容无法解析为 JSON")
    except Exception as e:
        LOG.warning("非流式兜底请求失败：%s", e)
    raise RuntimeError(f"所有候选端点（{len(cands)}）流式+非流式兜底均失败，最后错误：{last_err}")


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


def main():
    C.ensure_dirs()
    today = C.date_str()
    LOG.info("开始生成 %s 日报", today)

    # 1) 读取信号
    cand_path = os.path.join(DATA_DIR, f"candidates-{today}.json")
    candidates = C.load_json(cand_path, {})
    if isinstance(candidates, list):
        signals = candidates
    else:
        signals = candidates.get("candidates") or candidates.get("items") or []
    if len(signals) > MAX_INPUT_SIGNALS:
        # 均衡采样：按来源分组后每个源均匀取 ceil(上限/源数) 条，
        # 保证每个内容源都有代表进入 AI，避免「前源霸占、后源永远进不了 AI」。
        from collections import OrderedDict
        groups = OrderedDict()
        for s in signals:
            groups.setdefault(s.get("source_name", "未知"), []).append(s)
        n_src = len(groups) or 1
        per = max(1, -(-MAX_INPUT_SIGNALS // n_src))  # ceil
        picked, leftover = [], []
        for name, items in groups.items():
            if len(items) > per:
                picked.extend(items[:per])
                leftover.extend(items[per:])
            else:
                picked.extend(items)
        # 若均衡后仍不足上限（某源内容少），用剩余补足，优先补齐未达 per 的源
        if len(picked) < MAX_INPUT_SIGNALS and leftover:
            leftover.sort(key=lambda x: x.get("source_name", ""))
            picked.extend(leftover[: MAX_INPUT_SIGNALS - len(picked)])
        signals = picked[:MAX_INPUT_SIGNALS]
        LOG.info("候选 %d 条，按 %d 个源均衡采样至 %d 条送入 AI（每源约 %d 条）",
                 len(candidates) if isinstance(candidates, list) else len(signals) + len(leftover),
                 n_src, len(signals), per)
    LOG.info("读取到 %d 条候选信号", len(signals))

    if not signals:
        LOG.info("今日无候选信号，写空日报并结束。")
        C.save_json(os.path.join(DATA_DIR, f"report-{today}.json"), _empty_report(today))
        return

    # 2) 读取 prompt
    try:
        system_prompt = open(SKILL_FILE, "r", encoding="utf-8").read()
    except Exception as e:
        LOG.error("无法读取 SKILL.md：%s", e)
        raise

    user_prompt = (
        f"今天是 {today}（北京时间）。以下是已完成去重的当日增量信号（JSON）：\n"
        f"```json\n{json.dumps(signals, ensure_ascii=False, indent=2)}\n```\n"
        f"请按 SKILL v2 规则，输出 ai-sidehustle-report 日报 JSON。"
    )

    # 3) 调用 AI
    base_url = os.environ.get("AI_BASE_URL", "https://ai.jinbufenzi.com/v1")
    api_key = os.environ.get("AI_API_KEY", "")
    model = os.environ.get("AI_MODEL", "auto")

    if not api_key:
        LOG.error("缺少 AI_API_KEY（Secret AI_SIDEHUSTLE_API_KEY 未注入），无法生成。")
        raise SystemExit("missing AI key")

    # 3) 调用 AI（外层生成级重试：应对空生成 / 解析失败 / 结构不完整）
    content = None
    report = None
    for gen in range(1, GEN_RETRIES + 1):
        try:
            content = _call_ai(base_url, api_key, model, system_prompt, user_prompt)
        except Exception as e:
            LOG.error("AI 调用失败（第 %d/%d 次生成）：%s", gen, GEN_RETRIES, e)
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
    LOG.info("日报已生成：3 个模块共 %d 条，写入 %s", total, out_path)


if __name__ == "__main__":
    main()
