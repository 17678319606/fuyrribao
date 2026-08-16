#!/usr/bin/env python3
"""步骤2：读取当日去重信号，调用 AI 生成结构化日报 JSON。

高延迟优化（v3.0 / v4.2 Gemini 首选 / v4.3 原生 Gemini）：
- 移除降级兜底：AI 不可用则直接失败， workflow 随之失败，不会发布非 AI 内容。
- AI 后端首选 Google Gemini（原生 API）：海外 Runner 直连 Google 全球边缘，
  规避「海外 Runner → 国内网关」跨境瓶颈（根因）。
  自动兼容 AI Studio 新版 AQ. Auth key（不支持 OpenAI 兼容端点，只能用原生 Gemini API）。
- 可选 AI_BASE_URL_POOL 镜像 + AI_FALLBACK_URL（国内网关，OpenAI 兼容）兜底。
  每个端点含独立 (url, key, model)，并根据 host 自动选择原生 Gemini 或 OpenAI 兼容协议。
- 允许 AI_FORCE_NON_STREAM（强制非流式）、AI_REQUEST_TIMEOUT（覆盖读超时）以适配慢链路。
- 保留 DNS-over-HTTPS 兜底与代理池，用于海外 Runner 偶发解析抖动（仅国内网关等易抖动 host）。
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
import score  # 打分地基（通用层 + 主题层 yaml）；当前仅 SHADOW，不拦截

LOG = C.get_logger()


class _AbandonEndpoint(Exception):
    """流式空响应 / 整体超时且内容无法解析：放弃当前端点，转下一个端点或非流式兜底。

    默认情况下流式空响应会直接 raise 并逃逸出 _call_ai，导致末尾的「非流式兜底」
    永远没机会执行；用此异常在端点内层捕获并 break 到下一端点，提升网关抖动时的自愈率。
    """
    pass


SKILL_FILE = C.SKILL_FILE
DATA_DIR = C.DATA_DIR

RETRY_PER_ENDPOINT = 2      # 每端点重试：收敛防时间爆炸；冗余靠多镜像+DoH+代理池提供
BACKOFF_BASE = 8            # 退避基数 8s：网关高延迟，更长退避让其喘气（用户允许稍晚生成）
REQ_TIMEOUT = (20, 300)     # 默认读超时 300s；可被环境变量 AI_REQUEST_TIMEOUT 覆盖
GEN_RETRIES = 4             # 外层生成重试 4 次：用户要求必须 AI 输出、可稍晚，多给几次机会
STREAM_MAX_SECONDS = 900    # 单次流式读取整体 wall-clock 上限：防端点极慢吐数据/无 [DONE] 标记导致无限 hang
MAX_INPUT_SIGNALS = 50      # 送入 AI 的候选上限（控制上下文长度保稳定）
MAX_OUTPUT_TOKENS = 16000   # 输出 token 上限（收敛输出，降低超时/截断概率；
                              # 探针实测 35 候选大 prompt 在 6000 处被截断导致 JSON 残缺，
                              # 后提到 12000；实测大日报（条目多、字段全）仍会触顶导致
                              # 末尾条目/字段被截，故再提到 16000 留足余量；
                              # 流式回传下 524 风险仍可控；非流式兜底仅极偶发触发）

# —— 分批筛选（避免大量候选被直接截断丢弃，提升内容丰富度）——
SCREEN_THRESHOLD = 60     # 候选超过此数才启用分批筛选；否则全量直送生成（省调用、保速度）
SCREEN_BATCH = 60         # 每批送入筛选的候选数（平衡单批覆盖度与调用次数）

# —— Gemini 免费层限速器 ——
# Google AI Studio 免费层限制 15 RPM（每分钟 15 次请求）。
# screen_signals() 分批筛选会在几秒内连续打 ~10 次 AI 调用 → 直接撞 429。
# 本模块级变量记录上次 Gemini 原生调用时间，每次调用前自动补眠至最小间隔，
# 所有调用方（筛选循环 / 主生成 / daily_summary）统一受控，无需逐处改。
_gemini_last_call = 0.0   # 上次 _call_gemini_native 发起请求的 time.time()
GEMINI_RATE_INTERVAL = float(os.environ.get("GEMINI_RATE_INTERVAL", "4.0"))  # 默认 4s → ≤15 RPM
SCREEN_KEEP_PER_BATCH = 10  # 每批最多保留的精华条数（仅作日志参考，实际取汇总 TopN）
SCREEN_FINAL_CAP = 50     # 筛选后送入最终生成的最大条数（收敛上下文，减少 400/截断）
SCREEN_INPUT_CAP = 180    # 防御性上限：候选总量超过此数先按源均衡采样，避免一次性压垮 AI 端点（限流/超时）


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

    默认流式优先：GitHub Actions（海外 Runner）→ 国内网关场景下，非流式需等
    网关把整包（数千 token）生成完才回传，EdgeOne 等源站响应易超时（524）；
    流式边生成边回传，持续吐数据，不会触发 origin response timeout，更稳更快。
    仅在 AI_FORCE_NON_STREAM=1 时强制非流式（作为流式全失败后的兜底已内置）。
    """
    raw = os.environ.get("AI_FORCE_NON_STREAM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _trim_signals_for_prompt(signals, max_content=1500):
    """为 prompt 裁剪信号：保留完整元数据，content 截断到 max_content 字符。

    说明：此前默认 400 字过短——长文章 AI 只看到前 400 字，生成的「建议怎么做 /
    MVP」「变现说明」等字段会缺失后半段信息，表现为"长内容被轻微截断"。
    提到 1500 字：绝大多数文章的关键信息（信号、做法、数据）都在前 1500 字内，
    AI 据此可写出完整摘要；同时仍控制上下文，避免大 prompt 触发网关 400/超时。
    """
    out = []
    for s in signals:
        c = dict(s)
        content = c.get("content") or ""
        if len(content) > max_content:
            c["content"] = content[:max_content].rstrip() + "…（已截断）"
        out.append(c)
    return out


def _normalize_title(t):
    """标题归一化：用于跨源去重（同一文章被不同源转载时 URL 不同但标题相似）。
    小写 + 去空格/标点 + 去常见前后缀（如「实测」「上线」等 AI 生成前缀噪声）。"""
    if not t:
        return ""
    t = t.lower().strip()
    # 去除 HTML 实体
    import html as _html
    t = _html.unescape(t)
    # 去除标点与空白
    import re as _re
    t = _re.sub(r'[^\w\u4e00-\u9fff]', '', t)
    return t


def _prefilter_signals(signals):
    """送 AI 前的免费预筛（零 LLM 调用，不破免费额度）：去重 + 丢桩。

    - 去重（两层）：
      ① 同 item_dedup_key（source_url / title / content hash）只保留首次；
      ② 归一化标题去重：同一文章被不同源转载时 URL 不同但标题相似 → 仅保留一条；
    - 丢桩：标题+正文合计 < PREFILTER_MIN_LEN 且不含主题词的极短条目直接丢弃，
      降噪并节省后续 AI 调用额度。
    """
    seen, out = set(), []
    seen_titles = set()  # 归一化标题去重（防跨源重复）
    for s in signals:
        k = C.item_dedup_key(s)
        if k:
            if k in seen:
                continue
            seen.add(k)
        # 归一化标题去重（跨源同文不同 URL）
        norm_title = _normalize_title(s.get("title", ""))
        if norm_title and len(norm_title) >= 8:  # 短标题不参与标题去重（误杀风险高）
            if norm_title in seen_titles:
                continue
            seen_titles.add(norm_title)
        text = ((s.get("title") or "") + " " + (s.get("content") or "")).strip()
        if len(text) < C.PREFILTER_MIN_LEN and not any(
            kw.lower() in text.lower() for kw in C.SOURCE_RELEVANCE_KEYWORDS
        ):
            continue
        out.append(s)
    LOG.info("免费预筛：%d → %d（去重+丢桩，含标题归一化去重）", len(signals), len(out))
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
    """解析候选 base_url：兼容大写 AI_BASE_URL / 小写 ai_base_url（原项目璇玑网关约定）。
    显式设置优先；未设置时默认回退到国内网关 ai.jinbufenzi.com（用户原项目稳定可用的网关），
    而非海外 Gemini，避免「默认即限流」。再拼 AI_BASE_URL_POOL（分号/逗号/换行分隔）。"""
    explicit = (os.environ.get("ai_base_url", "").strip()
                or os.environ.get("AI_BASE_URL", "").strip())
    primary = explicit or "https://ai.jinbufenzi.com/v1"
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
    """仅对国内网关 jinbufenzi 等偶发解析失败的 host 做 DoH 兜底钉 IP；
    对 googleapis.com 等全球可达域名直接跳过（正常 DNS 更稳，避免单 IP 钉死）。"""
    if "jinbufenzi" not in host:
        return
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


def _is_gemini_native(base_url):
    """判断端点是否为 Google Gemini 原生 API（非 OpenAI 兼容层）。

    AI Studio 新版 AQ. Auth key 不支持 OpenAI 兼容端点，只能用原生 Gemini API：
    https://generativelanguage.googleapis.com/v1beta/...
    若 URL 包含 /openai/ 则视为 OpenAI 兼容层（旧 AIza key 才走这里）。
    """
    if not base_url:
        return False
    low = base_url.lower()
    return "generativelanguage.googleapis.com" in low and "/openai/" not in low


def _call_gemini_native(base_url, api_key, model, system_prompt, user_prompt, stream=True):
    """调用 Google Gemini 原生 API（适配 AI Studio AQ. Auth key）。

    - 端点示例：https://generativelanguage.googleapis.com/v1beta
    - 鉴权：URL 查询参数 ?key=API_KEY（AQ. Auth key 的标准用法）
    - 非流式：models/{model}:generateContent
    - 流式：models/{model}:streamGenerateContent（返回 JSON Lines）
    返回模型原始 content 字符串。
    """
    start = time.time()

    # 免费层限速：确保两次 Gemini 调用间隔 ≥ GEMINI_RATE_INTERVAL（默认 4s），
    # 避免分批筛选等循环密集调用撞 15 RPM 墙导致 429。
    global _gemini_last_call
    elapsed = time.time() - _gemini_last_call
    if elapsed < GEMINI_RATE_INTERVAL and _gemini_last_call > 0:
        wait = GEMINI_RATE_INTERVAL - elapsed
        LOG.info("Gemini 限速：距上次调用 %.1fs，补眠 %.1fs（间隔=%.1fs）",
                 elapsed, wait, GEMINI_RATE_INTERVAL)
        time.sleep(wait)
    _gemini_last_call = time.time()

    base_url = base_url.rstrip("/")
    model_id = model.split("/")[-1] if "/" in model else model
    action = "streamGenerateContent" if stream else "generateContent"
    url = f"{base_url}/models/{model_id}:{action}?key={api_key}"

    # Gemini 原生 API 把 system prompt 拼进 user contents 前面
    full_prompt = f"{system_prompt}\n\n{user_prompt}".strip()
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": full_prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }
    headers = {"Content-Type": "application/json"}
    timeout = _req_timeout()
    mode = "stream" if stream else "non-stream"

    # 免费层 Gemini 常见瞬时错误：503/502/504（Google 侧过载）、429（免费层限流）、空流。
    # 这些不是鉴权/配额硬失败，给 3 次指数退避重试，命中即省下兜底网关、提升主通道占比。
    GEMINI_TRANSIENT = (429, 500, 502, 503, 504)
    max_attempts = 3
    base_delay = 2.0
    last_err = None
    for attempt in range(1, max_attempts + 1):
        LOG.info("Gemini 原生请求 [base=%s, model=%s] (%s) 第 %d/%d 次",
                 base_url, model_id, mode, attempt, max_attempts)
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout, stream=stream)
            code = resp.status_code
            # 流式响应里 5xx 可能以非异常形式返回，这里先判状态码
            if code in GEMINI_TRANSIENT:
                last_err = RuntimeError(f"Gemini 原生瞬时服务端错误 {code}")
                wait = base_delay * attempt
                LOG.warning("Gemini 原生返回 %d（瞬时），%.1fs 后重试 (%d/%d)", code, wait, attempt, max_attempts)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            body = ""
            try:
                body = (e.response.text or "")[:400]
            except Exception:
                pass
            # AI Studio 新版 AQ. key 常见硬失败：预付费额度耗尽（非限流，充值/绑卡前无解）
            if code == 429 and any(k in body.lower() for k in ("prepayment", "credits", "depleted")):
                LOG.error("Gemini 原生调用被拒：预付费额度已耗尽（prepayment credits depleted），将退回兜底网关。"
                          "请在 Google AI Studio 项目 94038486169 充值或绑定计费账户后自动恢复。")
                raise
            if code in GEMINI_TRANSIENT:
                last_err = e
                wait = base_delay * attempt
                LOG.warning("Gemini 原生 HTTP %d（瞬时），%.1fs 后重试 (%d/%d)", code, wait, attempt, max_attempts)
                time.sleep(wait)
                continue
            raise

        if not stream:
            data = resp.json()
            text = ""
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    text += part.get("text", "")
            LOG.info("Gemini 原生请求成功（模式=non-stream，耗时 %.1fs，约 %d 字）",
                     time.time() - start, len(text))
            return text

        # 流式：JSON Lines，每行一个完整 JSON chunk
        content = ""
        stream_deadline = time.time() + STREAM_MAX_SECONDS
        last_progress = time.time()
        for raw_line in resp.iter_lines(decode_unicode=False):
            if time.time() > stream_deadline:
                LOG.warning("Gemini 流式读取整体超时（>%ds），强制中断", STREAM_MAX_SECONDS)
                break
            if not raw_line:
                continue
            try:
                line = raw_line.decode("utf-8").strip()
            except Exception:
                line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for cand in obj.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    delta = part.get("text", "")
                    if delta:
                        content += delta
            if (time.time() - last_progress > 60 or
                    (len(content) > 0 and len(content) % 500 < 50)):
                LOG.info("Gemini 流式读取中：已收 %d 字，耗时 %.1fs", len(content), time.time() - start)
                last_progress = time.time()

        if not content:
            # 空流多为瞬时（连接抖动 / Google 过载），按瞬重试；用尽后再降级兜底
            last_err = RuntimeError("Gemini 原生流式响应为空")
            wait = base_delay * attempt
            LOG.warning("Gemini 原生流式响应为空（瞬时?），%.1fs 后重试 (%d/%d)", wait, attempt, max_attempts)
            time.sleep(wait)
            continue
        LOG.info("Gemini 原生请求成功（模式=stream，耗时 %.1fs，约 %d 字）",
                 time.time() - start, len(content))
        return content

    # 重试耗尽：抛出让上层 _call_ai 降级到兜底网关（兜底逻辑不变）
    raise last_err or RuntimeError("Gemini 原生调用失败（瞬时重试耗尽）")


def _call_ai(base_urls, api_key, model, system_prompt, user_prompt, stream=True):
    """调用 AI，支持多 (url,key,model) 端点 fallback + 端点轮换 + 重试。
    返回模型原始 content 字符串。

    - base_urls 可为字符串或列表（首选，共用传入的 api_key/model）；
      另含可选 AI_FALLBACK_URL/KEY/MODEL 兜底端点（默认国内网关，独立 key/model）。
    - 首选即 Google Gemini 原生 API：海外 Runner 直连，规避跨境瓶颈；
      自动适配 AI Studio 新版 AQ. Auth key（不支持 OpenAI 兼容端点）。
    - AI_FORCE_NON_STREAM=1 时强制全部调用走非流式。
    - AI_REQUEST_TIMEOUT 可覆盖默认读超时。
    - 流式读取自带 STREAM_MAX_SECONDS 整体 wall-clock 上限，避免端点极慢吐数据或
      SSE 结束标记缺失导致无限挂起（这是 GitHub Runner 8 小时卡死的头号根因）。
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
    # enable_thinking 安全网：若网关拒绝 chat_template_kwargs 参数（400），
    # 置位后本调用内后续尝试去掉该参数重试，避免整端点失效。
    strip_thinking = False

    # 展开为 (url, key, model) 端点列表：首选 base_urls（共用 api_key/model）
    # + 可选兜底（AI_FALLBACK_URL/KEY/MODEL，默认国内网关，独立 key/model）。
    # 端点装配：默认首选传入的 base_urls（Gemini 等），可选 AI_FALLBACK_URL 作兜底。
    # 低摩擦增强：若检测到 DEEPSEEK_API_KEY，则把 DeepSeek（OpenAI 兼容、国内稳定、
    # 用户长期偏好）作为【首选】，原 base_urls 降级为兜底——避免在已限流(429)的 Gemini
    # 上反复重试浪费时间。只需在密钥仓库 env.yml 加一行 DEEPSEEK_API_KEY=<你的key> 即生效。
    endpoints = []
    seen = set()
    fb_url = os.environ.get("AI_FALLBACK_URL", "").strip().rstrip("/")
    fb_key = os.environ.get("AI_FALLBACK_KEY", "").strip()
    fb_model = os.environ.get("AI_FALLBACK_MODEL", "").strip() or model
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ds_model = os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
    # 仅当用户未显式指定 base_url（依赖默认网关）时才把 DeepSeek 自动插为首选，
    # 避免覆盖用户明确配置的 ai.jinbufenzi 等网关。
    user_set_base = (os.environ.get("AI_BASE_URL", "").strip()
                     or os.environ.get("ai_base_url", "").strip())
    ds_preferred = bool(ds_key) and not user_set_base

    for u in base_urls:
        u = (u or "").strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            endpoints.append((u, api_key, model))
    if fb_url and fb_url not in seen:
        endpoints.append((fb_url, fb_key, fb_model))
    if ds_preferred and "https://api.deepseek.com/v1" not in seen:
        # DeepSeek 插到最前作为首选；原有端点整体退为兜底。
        endpoints.insert(0, ("https://api.deepseek.com/v1", ds_key, ds_model))
        LOG.info("检测到 DEEPSEEK_API_KEY，已将 DeepSeek 设为首选端点，原 base_urls 降级为兜底")

    for (base_url, api_key, model) in endpoints:
        # Gemini 原生 API（适配 AI Studio AQ. Auth key）：直接调用，不走 OpenAI 兼容层/代理池
        if _is_gemini_native(base_url):
            try:
                return _call_gemini_native(base_url, api_key, model,
                                           system_prompt, user_prompt, stream)
            except Exception as e:
                LOG.warning("Gemini 原生调用失败（base=%s）：%s", base_url, e)
                last_err = e
                continue

        url = base_url.rstrip("/") + "/chat/completions"
        # 解析目标 host：仅国内网关等易抖动 host 才用 DoH 兜底钉 IP；
        # googleapis.com 等全球可达域名走正常 DNS 更稳。
        try:
            _host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(base_url).netloc \
                or "generativelanguage.googleapis.com"
        except Exception:
            _host = "generativelanguage.googleapis.com"
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
        # 国内网关 ai.jinbufenzi.com 默认模型为 auto（网关自动选最优模型）：
        # 复杂/长 prompt 会进入 thinking 阶段，慢且易被 EdgeOne 源站超时（524）截断，
        # 导致流式只收到 reasoning、content 为空 → 整批失败。关闭 thinking 可显著缩短生成、
        # 规避 524/空流，且 Qwen3 关闭思考后结构化输出仍正常。仅对 jinbufenzi 网关加此参数，
        # 避免误发给其他 OpenAI 兼容端点导致 400。
        if "jinbufenzi" in _host:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        for ci, proxy in enumerate(cands):
            proxies = {"http": proxy, "https": proxy} if proxy else None
            label = f"{base_url} | {proxy or '直连'}"
            for attempt in range(1, RETRY_PER_ENDPOINT + 1):
                # enable_thinking 安全网：本端点若曾被网关 400 拒绝该参数，则本次去掉后重试
                payload_now = dict(payload)
                if strip_thinking:
                    payload_now.pop("chat_template_kwargs", None)
                try:
                    LOG.info("AI 请求 [base=%s 端点 %d/%d=%s] 第 %d/%d 次尝试 (%s)",
                             base_url, ci + 1, len(cands), proxy or "直连",
                             attempt, RETRY_PER_ENDPOINT, mode)
                    resp = requests.post(
                        url, headers=headers, json=payload_now,
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
                    stream_deadline = time.time() + STREAM_MAX_SECONDS
                    last_progress = time.time()
                    for raw_line in resp.iter_lines(decode_unicode=False):
                        if time.time() > stream_deadline:
                            LOG.warning("流式读取整体超时（>%ds），强制中断并尝试用已收集内容继续",
                                        STREAM_MAX_SECONDS)
                            break
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
                        # 进度日志：每 60s 或每累积 500 字打印一次，方便观察是否还在活
                        if (time.time() - last_progress > 60 or
                                (len(content) > 0 and len(content) % 500 < len(delta or ""))):
                            LOG.info("流式读取中：已收 %d 字，耗时 %.1fs",
                                     len(content), time.time() - start)
                            last_progress = time.time()
                    if not content:
                        # 诊断：把原始响应前若干行打出来，便于判断是鉴权错误/Challenge/格式差异
                        snippet = " | ".join(raw_dump[:15])[:800]
                        LOG.error("流式响应为空，原始响应片段：%s", snippet)
                        # 不立刻崩溃：放弃当前端点，转非流式兜底 / 下一端点重试，提升网关抖动自愈率
                        raise _AbandonEndpoint("流式响应为空（见上方原始片段）")
                    # 校验 JSON 完整性：流式偶发被网关/CF 截断会导致内容不完整，
                    # 需当作本次尝试失败并退避重试，而非直接返回残缺内容
                    extracted = _extract_json(content)
                    if extracted is None:
                        # 若已因 wall-clock 超时被中断，内容仍不完整说明端点 SSE 异常，
                        # 继续在同端点重试只会再浪费 15 分钟，直接放弃该端点/模式。
                        if time.time() > stream_deadline:
                            LOG.error("流式读取超时且内容无法解析为 JSON，放弃该端点")
                            raise _AbandonEndpoint("stream wall-clock timeout with invalid JSON")
                        wait = BACKOFF_BASE * (2 ** (attempt - 1))
                        LOG.warning("AI 返回无法解析为 JSON（可能流式被截断），本次尝试失败，%ds 后重试",
                                    wait)
                        time.sleep(wait)
                        continue
                    # 如果是因为 wall-clock 超时中断的，但能解析出 JSON，也接受为成功
                    # （避免某些端点不发 [DONE] 但内容已完整的情况被误判为失败）
                    if time.time() > stream_deadline:
                        LOG.info("流式读取因整体超时被中断，但内容可解析为 JSON，视为成功")
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
                    # 限流 429：同一端点重试只会继续撞墙、耗尽额度且拖慢整体。
                    # 立即放弃该端点、转下一端点（兜底），让有配额的端点接管，而非死磕限流端点。
                    if status == 429:
                        LOG.warning("AI 接口返回 429 限流（base=%s），放弃该端点、转下一端点（兜底）", base_url)
                        raise _AbandonEndpoint(f"429 rate limit on {base_url}")
                    # 网关若不支持 chat_template_kwargs（400），去掉该参数重试一次，
                    # 避免整端点因一个可选参数被拒而失效（其他 OpenAI 兼容端点也可能不支持）。
                    if status == 400 and "chat_template_kwargs" in payload and not strip_thinking:
                        strip_thinking = True
                        LOG.warning("网关拒绝 enable_thinking 参数(400)，重试去掉 chat_template_kwargs：%s",
                                    body[:160])
                        time.sleep(BACKOFF_BASE)
                        continue
                    if _is_retryable_http(status):
                        # 尊重服务端 Retry-After（限流冷却），否则用指数退避
                        wait = _retry_after_seconds(e.response) or (BACKOFF_BASE * (2 ** (attempt - 1)))
                        # EdgeOne 源站超时(524)/5xx：后端可能过载，给更长冷却让其恢复，
                        # 避免 8s 内反复打满已过载的源站（网关抖动自愈的关键）。
                        if 500 <= status < 600:
                            wait = max(wait, 20)
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
                except _AbandonEndpoint as e:
                    # 流式空响应 / 整体超时且内容无效：放弃当前端点（及剩余代理），
                    # 转下一个端点；若已是最后端点，则 _call_ai 末尾的非流式兜底会接管。
                    last_err = e
                    LOG.warning("放弃当前端点（base=%s，端点=%s）：%s —— 转下一端点/非流式兜底",
                                base_url, proxy or "直连", e)
                    break
    # 非流式兜底：流式（海外链路偶发被截断/524）所有 base_url+端点均失败后，
    # 用同一组 url 做一次整包返回请求，作为最后手段。仍失败才真正报错。
    if stream:
        LOG.warning("流式多端点均失败，尝试非流式兜底（整包返回）一次…")
        for (base_url, api_key, model) in endpoints:
            if not api_key:
                continue
            url = base_url.rstrip("/") + "/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            nb_payload = {
                "model": model,
                "temperature": 0.5,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            # 与流式一致：jinbufenzi 网关关闭 thinking，避免整包生成被 524 截断
            try:
                _nb_host = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(base_url).netloc
            except Exception:
                _nb_host = ""
            if "jinbufenzi" in _nb_host and not strip_thinking:
                nb_payload["chat_template_kwargs"] = {"enable_thinking": False}
            try:
                r2 = requests.post(url, headers=headers, json=nb_payload,
                                   proxies=None, timeout=timeout)
                r2.raise_for_status()
                msg = (r2.json().get("choices", [{}])[0]
                       .get("message", {}).get("content", ""))
                if msg and _extract_json(msg) is not None:
                    LOG.info("非流式兜底成功（base=%s，约 %d 字）", base_url, len(msg))
                    return msg
                LOG.warning("非流式兜底返回内容无法解析为 JSON（base=%s）", base_url)
            except Exception as e:
                LOG.warning("非流式兜底请求失败（base=%s）：%s", base_url, e)
    raise RuntimeError(
        f"所有候选 base_url（{len(base_urls)}）与端点均失败，总耗时 %.1fs，最后错误：{last_err}" % (
            time.time() - start,))


def _parse_lenient_json(s):
    """对单段文本做容错 JSON 解析：直连 → 中文引号替换（双+单）→ 去 trailing comma/注释。"""
    for cand in (s,
                 s.replace("\u201c", '"').replace("\u201d", '"'),
                 s.replace("\u2018", "'").replace("\u2019", "'")):
        try:
            return json.loads(cand)
        except Exception:
            continue
    try:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", s)
        cleaned = re.sub(r"//[^\n]*", "", cleaned)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.S)
        return json.loads(cleaned)
    except Exception:
        return None


def _salvage_truncated(s):
    """流式被网关/链路截断（括号未闭合）时的兜底：
    逐模块扫描，抠出『完整』的条目对象，丢弃最后一个没写完的条目，
    重建一个合法（但可能偏短）的日报 JSON。连一条完整条目都抠不出则返回 None。
    即便只剩半篇，也能保住已完整生成的条目，避免整跑因截断而失败。"""
    modules = {k: [] for k in ("project_opportunities", "growth_operations", "views_insights")}
    n = len(s)
    for mod in modules:
        marker = '"%s":' % mod
        mi = s.find(marker)
        if mi == -1:
            continue
        i = mi + len(marker)
        while i < n and s[i] in " \t\n\r":
            i += 1
        if i >= n or s[i] != "[":
            continue
        i += 1
        in_str = False
        esc = False
        depth = 0
        buf = None
        while i < n:
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                i += 1
                continue
            if ch == '"':
                in_str = True
                i += 1
                continue
            if ch == "{":
                if depth == 0:
                    buf = i
                depth += 1
                i += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0 and buf is not None:
                    item = _parse_lenient_json(s[buf:i + 1])
                    if item is not None:
                        modules[mod].append(item)
                    buf = None
                i += 1
                continue
            i += 1
    if sum(len(v) for v in modules.values()) == 0:
        return None
    return {"date": "", "modules": modules, "daily_summary": {"methodology": "", "evidence": []}}


def _extract_json(text):
    """从模型输出里抠出 JSON（兼容 ```json 围栏/前后文字/中文引号/字符串内含括号/
    流式被截断兜底）。

    完整 JSON 优先返回；若流式被截断（括号未闭合），则降级到 _salvage_truncated
    抠出已完整生成的条目，最大限度保住成果、避免整跑失败。"""
    if not text:
        return None
    s = text.strip()
    if "```" in s:
        s = re.sub(r"```(?:json)?\s*", "", s)
        s = s.replace("```", "")
    start = s.find("{")
    if start == -1:
        return None
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
    if end != -1:
        parsed = _parse_lenient_json(s[start:end + 1])
        if parsed is not None:
            return parsed
    return _salvage_truncated(s[start:])



def _validate(report):
    assert isinstance(report, dict), "顶层不是对象"
    mods = report.get("modules", {})
    for key in ("project_opportunities", "growth_operations", "views_insights"):
        assert key in mods, f"缺少模块 {key}"
        assert isinstance(mods[key], list), f"{key} 不是数组"
        # 容错：AI 偶发把某条 item 返回成纯字符串（而非对象），清洗为合格字典，
        # 否则 render_item 调 .get() 会 AttributeError 导致整跑崩溃（v4 run 即此坑）。
        cleaned = []
        for idx, it in enumerate(mods[key]):
            if isinstance(it, dict):
                it.setdefault("title", "")
                it.setdefault("source_name", "")
                it.setdefault("source_url", "")
                it.setdefault("signal", "")
                cleaned.append(it)
            elif isinstance(it, str):
                LOG.warning("模块 %s 第 %d 条为字符串（非对象），已清洗为标题卡片", key, idx)
                cleaned.append({"title": it[:200], "source_name": "",
                                "source_url": "", "signal": ""})
        else:
            LOG.warning("模块 %s 第 %d 条类型异常（%s），已跳过", key, idx, type(it).__name__)
        if len(cleaned) > C.MAX_ITEMS_PER_MODULE:
            LOG.info("模块 %s 条目 %d 超出每模块上限 %d，截断至上限",
                     key, len(cleaned), C.MAX_ITEMS_PER_MODULE)
        mods[key] = cleaned[:C.MAX_ITEMS_PER_MODULE]
    assert "daily_summary" in report, "缺少 daily_summary"
    ds = report["daily_summary"]
    assert isinstance(ds, dict), "daily_summary 不是对象"
    # 容错：AI 偶发漏字段不致命，补默认值即可，避免整跑崩溃
    ds.setdefault("methodology", "")
    ds.setdefault("evidence", [])
    return True


# —— 空心条目检测 ——
import re

# v4.2：删除「按具体人名/融资词」硬匹配的正则（那是 AI 该干的，不该写死在代码里）。
# 主过滤交给 SKILL.md / 筛选 prompt 的「7 类共性 + 行动价值主闸门」让 AI 自评。
# 代码后处理只做「结构性兜底」（见 _is_hollow_item）：识别明显没填 actionable 字段的硬伤。

# 套话库：纯定性空话，无 actionable value
# v4.5：移除 "-"/"—"/"／" 等标点型"套话"——它们是正常行动字段（如"先在小红书发 3 篇 - 测试钩子"）
# 的组成部分，按裸子串匹配会误判整条行动字段为空心，导致 GOOD 内容被错误剔除（召回损失）。
# 套话只保留明确的定性空话短语。
_HOLLOW_FLUFF_LITERAL = ["N/A", "n/a", "暂无", "待定", "详见原文", "无",
                         "潜力巨大", "值得关注", "具有重要意义", "不可忽视",
                         "具有深远影响", "涌动", "频繁", "关注行业动态",
                         "可通过自媒体", "分享观点", "获取机会", "保持关注",
                         "拥抱变化", "抓住机遇", "顺势而为"]
_HOLLOW_FLUFF_REGEX = [r"反映.*发展", r"体现.*趋势", r"展示.*潜力", r"说明.*重要性",
                       r"随着.*推进", r"在.*背景下"]


def _has_fluff(text):
    """判断一段文字是否主要由套话构成（无具体 actionable 信息）。"""
    if not text:
        return True
    tl = text.lower()
    for f in _HOLLOW_FLUFF_LITERAL:
        if f.lower() in tl:
            return True
    for f in _HOLLOW_FLUFF_REGEX:
        if re.search(f, text):
            return True
    return False


def _is_hollow_item(item):
    """检测一条 item 是否为空心内容（对副业读者零 actionable value）。
    返回 (is_hollow: bool, reason: str)。
    """
    t = (item.get("title") or "") + " " + (item.get("signal") or "")
    mvp = (item.get("how_to_mvp") or "").strip()
    acq = (item.get("acquisition_channel") or "").strip()
    mon = (item.get("monetization") or "").strip()
    val = (item.get("value_proposition") or "").strip()
    per = (item.get("perspective") or "").strip()

    # v4.2：删除「按名字/融资词」硬排除（AI 自评负责）。此处只做结构性兜底：
    # 识别明显没填 actionable 字段的硬伤——真正判断力交给 AI 的 7 类共性框架。

    # 1) 核心行动字段全空或全为套话 → 空心（结构性）
    #    实质性 = 长度≥10 且不含套话；观点心法模块允许用 perspective/value_proposition 替代
    substantial = [f for f in (mvp, acq, mon) if len(f.strip()) >= 10 and not _has_fluff(f)]
    if len(substantial) == 0:
        is_views = (len(per) >= 20 and not _has_fluff(per)) or (len(val) >= 20 and not _has_fluff(val))
        if not is_views:
            return True, "核心行动字段(MVP/获客/变现)全空或全是套话，且观点/价值主张也不足"

    # 2) 所有字段过短且全为套话
    all_text = " ".join([mvp, acq, mon, val, per])
    if 0 < len(all_text) < 80:
        fluff_count = sum(1 for f in _HOLLOW_FLUFF_LITERAL if f.lower() in all_text.lower())
        if fluff_count >= 2:
            return True, f"所有字段过短({len(all_text)}字)且含{fluff_count}条套话"

    return False, ""


def _actionable_check(report):
    """生成后校验：扫描所有模块的 item，标记并剔除空心条目。返回 (cleaned_report, removed_count)。"""
    import re
    removed = 0
    mods = report.get("modules", {})
    for key in ("project_opportunities", "growth_operations", "views_insights"):
        items = mods.get(key, [])
        keep = []
        for it in items:
            hollow, reason = _is_hollow_item(it)
            if hollow:
                removed += 1
                LOG.warning("【空心剔除】%s -> %s | 标题: %s", key, reason,
                            (it.get("title") or "")[:60])
            else:
                keep.append(it)
        mods[key] = keep
    if removed > 0:
        LOG.warning("【行动性校验】共剔除 %d 条空心条目，剩余 %d 条",
                    removed, sum(len(m) for m in mods.values()))
    return report, removed


# —— 同玩法 / 同地域服务聚类去重（v4.3，L3 保险）——
# 目的：拦截"同一套玩法（相同服务品类 + 相同地域市场）在同一模块里重复堆砌≥3条"的冗余，
# 如"武汉 SEO/GEO 三连"。同时严格"地域中性"：单条地域内容（地域=获客渠道/目标客户定位）
# 永远不参与聚类、绝不降权；只有「同模块 + 同地域 + 同服务品类」≥3 条才收口，保留最具实质的 1 条。
# 识别维度（地域/服务品类）是通用类目，非"人名/关键词黑名单"，不违反"按共性筛、不打补丁"铁律。

# 通用地域类目（用于聚类维度，非排除名单）：覆盖主要城市/省份/区域
_REGION_TOKENS = [
    "北京", "上海", "广州", "深圳", "成都", "杭州", "武汉", "南京", "重庆", "西安",
    "苏州", "天津", "长沙", "郑州", "青岛", "厦门", "宁波", "无锡", "佛山", "合肥",
    "济南", "东莞", "福州", "昆明", "大连", "沈阳", "哈尔滨", "石家庄", "太原", "南昌",
    "南宁", "贵阳", "海口", "常州", "温州", "泉州", "绍兴", "嘉兴", "南通", "金华",
    "珠海", "惠州", "中山", "保定", "廊坊", "烟台", "兰州", "潍坊", "徐州", "临沂",
    "全国", "海外", "一线城市", "二三线城市",
]

# 通用服务/玩法类目映射（用于聚类维度，非排除名单）：多个近义 token 归并到同一类目，
# 避免"武汉 SEO"与"武汉 GEO"被当成不同玩法而漏聚；仅当这些词与地域同时出现且同模块≥3条
# 时才触发去重，绝不作为排除规则。
_SERVICE_MAP = {
    "seo": "SEO/GEO优化", "geo": "SEO/GEO优化", "搜索优化": "SEO/GEO优化", "搜索排名": "SEO/GEO优化",
    "代运营": "代运营", "建站": "建站/官网", "官网": "建站/官网", "小程序": "小程序",
    "公众号": "公众号运营", "私域": "私域/引流", "引流": "私域/引流", "涨粉": "私域/引流",
    "带货": "带货/直播", "直播": "带货/直播", "培训": "培训/课程", "课程": "培训/课程",
    "知识付费": "培训/课程", "付费社群": "培训/课程", "矩阵": "矩阵/账号",
    "外包": "外包/代办", "代办": "外包/代办", "中介": "中介", "saas": "SaaS",
}


def _item_text(it):
    if not isinstance(it, dict):
        return ""
    parts = []
    for k in ("title", "signal", "target_customer", "value_proposition",
              "how_to_mvp", "acquisition_channel", "monetization", "startup_cost",
              "replicability", "perspective"):
        v = it.get(k)
        if v:
            parts.append(str(v))
    return " ".join(parts)


def _detect_region(text):
    tl = text.lower()
    for r in _REGION_TOKENS:
        if r.lower() in tl:
            return r
    return ""


def _detect_service(text):
    tl = text.lower()
    for s in _SERVICE_MAP:
        if s.lower() in tl:
            return _SERVICE_MAP[s]
    return ""


def _cluster_dedup(report, min_cluster=3, keep=1):
    """L3 保险：同模块 + 同地域 + 同服务品类 的三元组，若 ≥ min_cluster 条，
    仅保留最具实质（字段文本最长）的 keep 条，其余标记「聚类冗余」剔除。
    地域中性：单条地域内容（无同模块同服务≥3）永不触达；地域仅作为签名的一个中性成分。"""
    removed = 0
    mods = report.get("modules", {})
    for mod in C.MODULES:
        items = mods.get(mod, [])
        if len(items) < min_cluster:
            continue
        groups = {}
        for idx, it in enumerate(items):
            txt = _item_text(it)
            region = _detect_region(txt)
            service = _detect_service(txt)
            # 仅当「地域 + 服务」同时命中才参与聚类；纯地域内容或纯话题内容不聚类
            if not (region and service):
                continue
            sig = "%s|%s|%s" % (mod, region, service)
            groups.setdefault(sig, []).append(idx)
        drop = set()
        for sig, idxs in groups.items():
            if len(idxs) >= min_cluster:
                # 保留字段文本最长的 keep 条（最具实质），其余剔除
                ranked = sorted(idxs, key=lambda i: len(_item_text(items[i])), reverse=True)
                for i in ranked[keep:]:
                    drop.add(i)
                    removed += 1
                    LOG.warning("【聚类冗余剔除】%s -> 同模块+同地域+同服务≥%d 条，保留最具实质 %d 条 | 标题: %s",
                                mod, min_cluster, keep, (items[i].get("title") or "")[:60])
        if drop:
            mods[mod] = [it for i, it in enumerate(items) if i not in drop]
    return report, removed


def _title_dedup_report(report):
    """L4 标题归一化去重：跨模块/同模块内，归一化标题完全相同或高度相似的条目仅保留一条。

    防御场景：
    - 同一文章被不同 RSS 源转载（URL 不同、标题微调但实质相同）；
    - AI 生成阶段对相似话题产出重复条目（如 Saathi/MoneyBuddy 同模板评语）。
    策略：保留字段文本最长的（最具实质），其余剔除。
    """
    removed = 0
    mods = report.get("modules", {})
    # 全局去重（跨模块也去重，避免同一文章出现在两个模块）
    all_items = []  # (mod_name, index, item)
    for mod in C.MODULES:
        items = mods.get(mod, [])
        for i, it in enumerate(items):
            all_items.append((mod, i, it))

    seen_titles = {}  # norm_title -> (mod, idx, text_len)
    drop = set()  # (mod, idx) to remove
    for mod, idx, it in all_items:
        norm = _normalize_title(it.get("title", ""))
        if not norm or len(norm) < 8:
            continue  # 短标题跳过
        text_len = len(_item_text(it))
        if norm in seen_titles:
            prev_mod, prev_idx, prev_len = seen_titles[norm]
            # 保留更长的（更具实质）
            if text_len > prev_len:
                drop.add((prev_mod, prev_idx))
                seen_titles[norm] = (mod, idx, text_len)
            else:
                drop.add((mod, idx))
            removed += 1
            LOG.warning("【标题重复】'%s' 与已有条目重复，剔除较短者(%d字 vs %d字)",
                        (it.get("title") or "")[:60], text_len, prev_len)
        else:
            seen_titles[norm] = (mod, idx, text_len)

    # 执行删除
    for mod, idx in drop:
        items = mods.get(mod, [])
        if 0 <= idx < len(items):
            items.pop(idx)
            # 后续索引需要调整——简单方案：重建列表时跳过已删

    # 重建：由于 pop 会改变索引，用标记方式更安全
    for mod in C.MODULES:
        items = mods.get(mod, [])
        drop_idxs = {idx for m, idx in drop if m == mod}
        if drop_idxs:
            mods[mod] = [it for i, it in enumerate(items) if i not in drop_idxs]

    return report, removed





# 画布字段 key（与 publish_wp.CANVAS_FIELDS 对应）；用于骨架判定
_CANVAS_KEYS = (
    "signal", "target_customer", "value_proposition", "how_to_mvp",
    "acquisition_channel", "monetization", "startup_cost",
    "replicability", "perspective",
)


def _analyze_richness(report):
    """统计「骨架程度」：返回 (items_total, items_zero_field, field_instances)。

    - items_zero_field：一个画布字段都没填的 item 数（纯标题卡片，无信息量）；
    - field_instances：所有 item 的画布字段非空实例总数。
    用于拦截 AI 偶发的「只写标题不写字段」骨架输出，避免发布空壳日报。
    """
    items_total = 0
    items_zero_field = 0
    field_instances = 0
    for key in ("project_opportunities", "growth_operations", "views_insights"):
        for it in report.get("modules", {}).get(key, []):
            if not isinstance(it, dict):
                continue
            items_total += 1
            filled = 0
            for fk in _CANVAS_KEYS:
                v = it.get(fk)
                if v and str(v).strip():
                    filled += 1
            if filled == 0:
                items_zero_field += 1
            field_instances += filled
    return items_total, items_zero_field, field_instances


def _is_skeleton(report):
    """骨架判定：纯标题、无实质字段内容的日报视为不合格。

    规则：① 存在 item 但「零字段 item 占比 > 30%（且多于 1 条）」→ 骨架；
         ② 平均每个 item 字段数 < 1 → 骨架（信息密度过低）。
    两者皆非才放行。
    """
    total, zero, inst = _analyze_richness(report)
    if total == 0:
        return False  # 无 item 由上层 total==0 逻辑处理
    if zero > max(1, int(total * 0.3)):
        return True
    if inst < total:
        return True
    return False


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
        "你是「副业日报」的内容守门员。你的第一原则不是「挑出相关的」，而是「挡掉无用的」。\n\n"
        "## 唯一主闸门：「读者价值测试」\n"
        "对每条候选，先问：一个想搞副业/做项目/拉新获客/省钱避坑的普通人，读完能具体地做一件什么事？\n"
        "- 能答出具体动作（项目点子/工具用法/操作步骤/可复用方法）→ 才考虑留下；\n"
        "- 答不出、或只能答「了解一下」「关注一下」→ **直接丢弃，不给分**。\n\n"
        "## 七类垃圾共性（按「特征」识别，不按「名字」——换一批人名照样拦得住）\n"
        "命中任一类且无「可迁移的具体动作」→ score=0，丢弃：\n"
        "1) 名人大佬的人事动态：主语是名人/员工，谓语是离职/入职/跳槽/升任/加盟/官宣创业/创办；\n"
        "2) 融资/并购/IPO 快讯：完成 X 轮融资 N 亿、获 Y 投资、被收购、递交招股书（数字再大也是别人的钱）；\n"
        "3) 泛科技/产品发布资讯：某模型/框架/App 发新版本、某大厂发新品（无副业怎么用/怎么赚的落地角度）；\n"
        "4) 花边/情绪/社会观察：名人私生活、行业情绪、社会话题（如「顶尖人才不敢结婚」）；\n"
        "5) 行情/宏观播报：股价/币价/金价/油价/CPI/降息降准/政策原文；\n"
        "6) 蹭热点无方法：标题吸睛（「XX 这么火，普通人如何入局」），正文只有方向没有步骤；\n"
        "7) 正确的废话/无信息增量：复述常识（「副业要选擅长的」「坚持很重要」）或空话（「潜力巨大」「值得关注」「反映发展」）。\n\n"
        "## 元规则（判断垃圾的核心）\n"
        "- 垃圾内容大多是**第三人称、描述性、被动接收**（「X 做了 Y」）；\n"
        "- 好内容大多是**第二人称、操作性、可迁移**（「你可以做 Z，用工具 W，先测 N 个用户」）；\n"
        "- 垃圾零摩擦就能写（随便描述一件事）；好内容带**具体摩擦**（真名、数据、步骤、工具、平台、数字）。\n\n"
        "## 反面教材（全部判【丢弃】）\n"
        "- 「Grok 4.6 上了牌桌」→ 泛科技/蹭热点，无副业操作步骤；\n"
        "- 「Suno Studio 2.0」（仅产品发新版）→ 泛科技资讯，未给低门槛可复制动作；\n"
        "- 「Why Making AI Answer Faster Is Worth $1.5 Billion」→ 融资数字快讯，读者无法据此做事；\n"
        "- 「顶尖 AI 人才，不敢结婚」→ 花边/情绪，零行动价值；\n"
        "- 「选择主业要考虑前途，副业才能突破」→ 正确废话，复述常识无步骤。\n\n"
        "## 评分标准（1-10）\n"
        "- 9-10：有具体项目案例 + 真实收入数据 + 可复用步骤（读者照着就能试）；\n"
        "- 7-8：有明确方法论或工具推荐，读者能学到东西并应用；\n"
        "- 5-6：有一定启发但偏泛，需要读者自己脑补很多细节；\n"
        "- 3-4：沾边但信息增量低，属于「知道了也没什么用」；\n"
        "- 1-2：勉强相关但基本是凑数的；\n"
        "- 0：不相关或上述七类垃圾。\n\n"
        "## v4.3 新增（同玩法聚类 + 信息增量硬门槛 + 第三人称豁免 + 地域中性）\n"
        "8) 同玩法/同窄地域服务聚类冗余：同一套玩法（相同服务品类 + 相同地域市场）重复出现≥3条、后出现的只是换说法重复同一 offer → score=0，丢弃（仅保留最有信息增量的一条）。\n"
        "信息增量硬门槛：通过项必须至少命中一个真料标签（真平台/真工具/真产品名、具体数字、≥2步步骤、可定位人群/场景），否则判⑦正确废话。\n"
        "第三人称转化豁免收紧：第三人称报道（某大厂人/某公司想做X）仅当提炼卡含≥1个非套话核心行动字段(MVP/获客/变现)且≥1个真料标签才通过，否则归①直接剔除。\n"
        "地域中性铁律：地域永远不是负信号；地域=获客渠道/目标客户定位的增长内容100%保留，绝不因出现地域词单独降权；只有同模块+同地域+同服务品类≥3条才按第8类聚类收口。\n\n"
        "只输出 JSON：{\"picks\":[{\"id\":\"与输入完全一致的原始 id\","
        "\"score\":1-10,\"reason\":\"一句话理由（必须说明为什么对副业读者有具体价值）\"}]}。"
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
    保证每个内容源都有机会被看到；筛选失败的批次保留原始候选，避免整批丢失
    导致内容丰富度骤降（降级为「未筛选直送」，最终仍受 SCREEN_FINAL_CAP 约束）。"""
    import random
    random.shuffle(signals)  # 打乱，使每批源混合，避免整批同源于是漏选
    batches = [signals[i:i + SCREEN_BATCH] for i in range(0, len(signals), SCREEN_BATCH)]
    by_id = {s["id"]: s for s in signals if s.get("id")}
    scored = {}
    skipped_raw = []   # 筛选失败的批次：保留原始候选，降级为未筛选直送
    t0 = time.time()
    for bi, batch in enumerate(batches):
        bs = time.time()
        try:
            picks = _screen_one_batch(batch, base_urls, api_key, model, system_prompt)
        except Exception as e:
            LOG.warning("分批筛选第 %d/%d 批失败，该批 %d 条保留为未筛选候选：%s",
                        bi + 1, len(batches), len(batch), e)
            skipped_raw.extend(batch)
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
    kept = [by_id[pid] for pid, _ in ranked]
    # 合并精选 + 未筛选候选，按 id 去重，最终统一截断到安全上限
    seen_ids, merged = set(), []
    for s in kept + skipped_raw:
        sid = s.get("id")
        if sid and sid in seen_ids:
            continue
        if sid:
            seen_ids.add(sid)
        merged.append(s)
    merged = merged[:SCREEN_FINAL_CAP]
    LOG.info("分批筛选完成：%d 候选 → %d 批 → 精选 %d + 未筛选 %d → 合并 %d → 取前 %d 条进生成，总耗时 %.1fs",
             len(signals), len(batches), len(kept), len(skipped_raw), len(merged),
             len(merged), time.time() - t0)
    return merged


def _emit_changed(changed):
    """写入状态标记，供 publish_wp.py 判断是否真的需要更新 WP（避免无变化时冗余发布）。"""
    try:
        flag = os.path.join(C.STATE_DIR, ".gen_changed")
        with open(flag, "w", encoding="utf-8") as f:
            f.write("1" if changed else "0")
    except Exception:
        pass


def _ensure_daily_summary(report, base_urls, api_key, model, today):
    """末波保证 daily_summary 完整：主生成若被截断/遗漏（daily_summary 在 JSON 尾部最易被掐），
    单独补一次聚焦生成，确保『完整日报』含『每日总结·可复用方法论』。非末波不要求，直接跳过。"""
    if not isinstance(report, dict):
        return
    ds = report.get("daily_summary")
    if not isinstance(ds, dict):
        ds = {}; report["daily_summary"] = ds
    if (ds.get("methodology") or "").strip():
        return  # 已有实质总结，无需补
    items = []
    for m in C.MODULES:
        for it in (report.get("modules", {}) or {}).get(m, []):
            items.append(it)
    if not items:
        return
    if not api_key:
        LOG.warning("无 AI key，无法补生成 daily_summary，保留空总结。")
        return
    ctx = json.dumps(
        [{"title": it.get("title", ""), "summary": (it.get("summary") or "")[:200],
          "source_url": it.get("source_url", "")} for it in items],
        ensure_ascii=False, indent=2)
    sys_p = ("你是副业日报的内容编辑。基于已收录条目，产出当天可复用方法论总结，"
             "严格围绕副业赚钱/省钱/做项目创业/增长运营，不写新闻时事。只输出 JSON。")
    user_p = (
        f"今天是 {today}（北京时间）。以下是今日副业日报已收录的 {len(items)} 条内容"
        f"（三模块：项目机会库/增长运营/观点心法）：\n{ctx}\n"
        f"请提炼一段 200-400 字『每日总结·可复用方法论』：总结今天在副业赚钱、省钱、做项目创业、"
        f"增长运营上的共性规律与可复用方法论；并从上述条目里挑选 3-5 条最具代表性的 source_url 作为 evidence。"
        f"\n只输出 JSON：{{\"daily_summary\":{{\"methodology\":\"（200-400字）\",\"evidence\":[\"url1\",\"url2\"]}}}}"
    )
    for gen in range(1, GEN_RETRIES + 1):
        try:
            content = _call_ai(base_urls, api_key, model, sys_p, user_p, stream=True)
        except Exception as e:
            LOG.error("daily_summary 补生成调用失败（%d/%d）：%s", gen, GEN_RETRIES, e)
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE); continue
            return
        sub = _extract_json(content)
        sub_ds = (sub.get("daily_summary") if isinstance(sub, dict) else None) or {}
        meth = (sub_ds.get("methodology") or "").strip()
        if not meth:
            if gen < GEN_RETRIES:
                time.sleep(BACKOFF_BASE); continue
            return
        ev = sub_ds.get("evidence") or []
        if not isinstance(ev, list):
            ev = []
        ev = [u for u in ev if isinstance(u, str) and u.startswith("http")][:5]
        report["daily_summary"] = {"methodology": meth, "evidence": ev}
        LOG.info("daily_summary 已补生成（%d字，%d条证据）", len(meth), len(ev))
        return
    LOG.warning("daily_summary 补生成多次失败，保留空总结（不影响其余条目发布）。")


def main():
    overall_t0 = time.time()
    C.ensure_dirs()
    today = C.date_str()
    LOG.info("开始生成 %s 日报", today)

    # 末波判定：优先用 workflow 注入的 DOCGEN_IS_FINAL；未注入时按北京时间回退判定。
    # 末波（19:01 及之后 / 早于 06:00 / 手动触发）需要输出「覆盖全天」的心法总结。
    _isf_env = os.environ.get("DOCGEN_IS_FINAL", "").strip()
    if _isf_env in ("1", "true", "True"):
        is_final = True
    elif _isf_env in ("0", "false", "False"):
        is_final = False
    else:
        _bjh = C.beijing_now().hour
        is_final = (os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch") \
            or _bjh >= 19 or _bjh < 6
    LOG.info("本波是否末波(is_final)=%s", is_final)

    # AI 配置与候选端点
    # 兼容多套命名：GEMINI_API_KEY/AI_API_KEY/ai_api_key（历史+原项目小写约定）；
    # AI_SIDEHUSTLE_API_KEY / AI_FALLBACK_KEY（兜底）。模型同理 AI_MODEL/ai_model。
    base_urls = _parse_base_urls()
    _first_base = (base_urls[0] if base_urls else "")
    # 主用 key 必须与网关配对，绝不能拿 A 家 key 打 B 家网关（否则 401/鉴权失败）：
    # - 显式设置了网关(ai_base_url/AI_BASE_URL) → 优先用与其配对的主 key(AI_API_KEY/ai_api_key)，
    #   仅当显式网关确为 Gemini 时才回落 GEMINI_API_KEY；
    # - 否则默认走璇玑国内网关 → 用璇玑兼容 key(AI_API_KEY/ai_api_key/AI_SIDEHUSTLE_API_KEY)，
    #   GEMINI_API_KEY 不参与（它打璇玑必失败）。
    _explicit_base = bool(os.environ.get("ai_base_url", "").strip()
                          or os.environ.get("AI_BASE_URL", "").strip())
    if _explicit_base:
        api_key = (os.environ.get("AI_API_KEY", "").strip()
                   or os.environ.get("ai_api_key", "").strip())
        if not api_key and "generativelanguage" in _first_base:
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    else:
        api_key = (os.environ.get("AI_API_KEY", "").strip()
                   or os.environ.get("ai_api_key", "").strip()
                   or os.environ.get("AI_SIDEHUSTLE_API_KEY", "").strip())
    # 兜底端点 key（仅当 AI_FALLBACK_URL 存在时才会被组装成端点）
    fallback_key = (os.environ.get("AI_SIDEHUSTLE_API_KEY", "").strip()
                    or os.environ.get("AI_FALLBACK_KEY", "").strip()
                    or os.environ.get("ai_api_key", "").strip())
    # 模型默认值随网关自适应：国内网关 ai.jinbufenzi.com 默认 auto（网关自动选模型）；
    # 海外 Gemini 默认 gemini-flash-latest。用户可用 AI_MODEL/ai_model 显式覆盖。
    _default_model = "gemini-flash-latest"
    if "jinbufenzi" in _first_base:
        _default_model = "auto"
    model = (os.environ.get("AI_MODEL", "").strip() or os.environ.get("ai_model", "").strip()
             or _default_model)
    if _force_non_stream():
        LOG.info("强制非流式模式：所有 AI 调用使用整包返回（可在 Secret AI_FORCE_NON_STREAM=0 关闭）")

    # 为所有候选 base_url 预做 DNS 补丁（避免首次调用时才解析）
    for u in base_urls:
        try:
            _h = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(u).netloc or "ai.jinbufenzi.com"
        except Exception:
            _h = "ai.jinbufenzi.com"
        _install_dns_patch(_h)

    # 1) 读取当日候选信号
    cand_path = os.path.join(DATA_DIR, f"candidates-{today}.json")
    candidates = C.load_json(cand_path, {})
    if isinstance(candidates, list):
        signals = candidates
    else:
        signals = candidates.get("candidates") or candidates.get("items") or []

    # 2) 载入当日已累积日报（state/ 已缓存，跨次运行保留）—— 同日多次触发【增量累积】
    daily_state_path = os.path.join(C.STATE_DIR, f"daily_report_{today}.json")
    clean_mode = os.environ.get("DOCGEN_CLEAN", "").strip() in ("1", "true", "True")
    if clean_mode:
        # 清洁模式：忽略历史累积，全新生成。用于覆盖重写旧文（如旧策略残留垃圾），
        # 无需再手工删 state 缓存——即使缓存里已有当日累积日报，也强制从空起步，
        # 杜绝「旧垃圾被原样 merge 回来」导致清洁失败。
        accumulated = {}
        LOG.info("清洁模式(DOCGEN_CLEAN)开启：忽略历史累积，全新生成（用于覆盖重写旧文）")
    else:
        accumulated = C.load_json(daily_state_path, {})
    acc_modules = accumulated.get("modules", {}) if isinstance(accumulated, dict) else {}
    existing_keys = set()
    for mod in C.MODULES:
        for it in acc_modules.get(mod, []):
            existing_keys.add(C.item_dedup_key(it))

    # 送 AI 前的免费预筛（去重 + 丢桩，零 LLM）
    signals = _prefilter_signals(signals)

    # 本次新增（未被当日累积过的）信号
    new_signals = [s for s in signals if C.item_dedup_key(s) not in existing_keys]

    # 渲染器版本或强制覆盖 → 仅重渲染，不重新调 AI
    force_rerender = os.environ.get("DOCGEN_FORCE_RERENDER", "").strip() in ("1", "true", "True")

    acc_total = sum(len(acc_modules.get(m, [])) for m in C.MODULES)
    changed = bool(new_signals) or force_rerender
    # 末波：即便本波无新增，也要基于已收录内容重算"全天心法总结"，故视为需生成
    if is_final and acc_total > 0:
        changed = True

    if not changed:
        LOG.info("今日无新增信号（已累积 %d 条）且无渲染器更新，跳过生成与发布。", acc_total)
        _emit_changed(False)
        return

    if not new_signals and force_rerender:
        if acc_total == 0:
            LOG.info("无新增信号且无已累积内容，渲染器更新无需操作。")
            _emit_changed(False)
            return
        LOG.info("无新增信号，但检测到渲染器版本更新，仅用已累积 %d 条内容重渲染。", acc_total)
        report = accumulated
    else:
        if not api_key and not fallback_key:
            LOG.error("缺少任何 AI key（未配置 GEMINI_API_KEY/AI_API_KEY/ai_api_key 或 AI_SIDEHUSTLE_API_KEY/AI_FALLBACK_KEY），无法生成。")
            raise SystemExit("missing AI key")

        # 候选编排（仅对新信号）：候选过多 → 分批筛选；中小批量 → 均衡采样；最后统一上限。
        # 防御性收敛：候选总量过大（如冷启动多源聚合 600+ 条）会一次性压垮 AI 端点
        # （限流/超时），先按源均衡采样到安全上限，保证每个源仍有机会被看到。
        if len(new_signals) > SCREEN_INPUT_CAP:
            import random
            from collections import OrderedDict
            random.shuffle(new_signals)
            groups = OrderedDict()
            for s in new_signals:
                groups.setdefault(s.get("source_name", "未知"), []).append(s)
            sampled, total0 = [], len(new_signals)
            while len(sampled) < SCREEN_INPUT_CAP and any(groups.values()):
                for nm in list(groups.keys()):
                    if groups[nm]:
                        sampled.append(groups[nm].pop(0))
                        if len(sampled) >= SCREEN_INPUT_CAP:
                            break
            new_signals = sampled
            LOG.info("候选总量 %d 超安全上限 %d，按源均衡采样至 %d 条（避免一次性压垮 AI 端点）",
                     total0, SCREEN_INPUT_CAP, len(new_signals))

        if len(new_signals) > SCREEN_THRESHOLD:
            new_signals = screen_signals(new_signals, base_urls, api_key, model, _screen_system_prompt())
        elif len(new_signals) > MAX_INPUT_SIGNALS:
            from collections import OrderedDict
            groups = OrderedDict()
            for s in new_signals:
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
            new_signals = picked[:MAX_INPUT_SIGNALS]
            LOG.info("新候选 %d 条，按 %d 个源均衡采样至 %d 条", len(new_signals), n_src, len(new_signals))
        if len(new_signals) > SCREEN_FINAL_CAP:
            new_signals = new_signals[:SCREEN_FINAL_CAP]
        LOG.info("最终送入生成：%d 条新增候选信号", len(new_signals))

        # 读取 prompt
        try:
            system_prompt = open(SKILL_FILE, "r", encoding="utf-8").read()
        except Exception as e:
            LOG.error("无法读取 SKILL.md：%s", e)
            raise

        # 为最终生成裁剪信号：保留元数据，content 截断到 1500 字符
        prompt_signals = _trim_signals_for_prompt(new_signals, max_content=1500)
        _MOD_TITLE = {
            "project_opportunities": "项目机会库",
            "growth_operations": "增长运营",
            "views_insights": "观点心法",
        }
        if is_final and acc_total > 0:
            # 末波：把"已收录"条目按模块分组作为上下文喂给 AI，让其输出完整 modules
            # （已收录 + 新增去重）且 daily_summary 覆盖全天；条数上限 = min(60, 已有+30)
            wave_cap = min(60, acc_total + 30)
            acc_parts = []
            for m in C.MODULES:
                items = acc_modules.get(m, [])
                if items:
                    acc_parts.append(
                        "【已收录-%s】\n%s" % (
                            _MOD_TITLE.get(m, m),
                            json.dumps(items, ensure_ascii=False, indent=2),
                        )
                    )
            acc_block = "\n\n".join(acc_parts) if acc_parts else "（无）"
            new_block = json.dumps(prompt_signals, ensure_ascii=False, indent=2)
            user_prompt = (
                f"今天是 {today}（北京时间）。本日报每天两波更新，现在是【末波】。\n"
                f"【已收录条目】（今日早些时候已纳入日报，请保留并在最后「每日总结」中一并覆盖，勿重复收录）：\n"
                f"{acc_block}\n"
                f"【本次新增待筛选信号】（JSON，请从中严格筛选符合主题与质量门槛的条目，补充进各模块）：\n"
                f"```json\n{new_block}\n```\n"
                f"请严格按 SKILL.md（v4.3 老创业人 · 严格筛选版（AI 自评过滤·按共性筛·同玩法聚类））规则，输出【完整】日报 JSON："
                f"已收录 + 新增 去重合并，三模块总条数 ≤ {wave_cap} 条（按质量分配，不平均）；"
                f"daily_summary.methodology 必须覆盖【全部】条目（已收录 + 新增）提炼出的一天可复用方法论。"
                f"生成前请再自检一次：每条的 MVP/获客/变现/观点 是否真的给了读者可执行的动作？"                f"凡是「关于别人的事（第三人称描述）」「只有方向没步骤」「全是形容词没数字」的条目，"                f"不要写进日报，直接跳过——但本批候选多已通过初筛、基本对读者有参考价值，默认应保留并展开为卡片；目标是产出有实质内容的日报，不要整体留空——除非确实全是垃圾，否则至少保留明显达标的几条。"
            )
        elif is_final and acc_total == 0:
            # 末波但无早波收录（早波漏跑）：当作全天处理，上限 60
            wave_cap = 60
            new_block = json.dumps(prompt_signals, ensure_ascii=False, indent=2)
            user_prompt = (
                f"今天是 {today}（北京时间）。本日报两波更新，今日首波即末波（此前无早波收录）。\n"
                f"以下是当日【新增】信号（JSON）：\n```json\n{new_block}\n```\n"
                f"请严格按 SKILL.md（v4.3 老创业人 · 严格筛选版（AI 自评过滤·按共性筛·同玩法聚类））规则，输出日报 JSON："
                f"三模块总条数 ≤ {wave_cap} 条（按质量分配，不平均，可某模块为空），宁缺毋滥。"
                f"生成前请再自检一次：每条的 MVP/获客/变现/观点 是否真的给了读者可执行的动作？"                f"凡是「关于别人的事（第三人称描述）」「只有方向没步骤」「全是形容词没数字」的条目，"                f"不要写进日报，直接跳过——但本批候选多已通过初筛、基本对读者有参考价值，默认应保留并展开为卡片；目标是产出有实质内容的日报，不要整体留空——除非确实全是垃圾，否则至少保留明显达标的几条。"
            )
        else:
            # 首波（非末波）：只处理本波新增，上限 30
            wave_cap = 30
            new_block = json.dumps(prompt_signals, ensure_ascii=False, indent=2)
            user_prompt = (
                f"今天是 {today}（北京时间）。以下是已完成去重的当日【新增】信号（JSON），"
                f"请仅基于这些新增信号生成条目，勿重复已有内容：\n"
                f"```json\n{new_block}\n```\n"
                f"请严格按 SKILL.md（v4.3 老创业人 · 严格筛选版（AI 自评过滤·按共性筛·同玩法聚类））规则，输出 ai-sidehustle-report 日报 JSON。"
                f"本波三模块总条数 ≤ {wave_cap} 条（按质量分配，不平均，可某模块为空），宁缺毋滥。"
                f"生成前请再自检一次：每条的 MVP/获客/变现/观点 是否真的给了读者可执行的动作？"                f"凡是「关于别人的事（第三人称描述）」「只有方向没步骤」「全是形容词没数字」的条目，"                f"不要写进日报，直接跳过——但本批候选多已通过初筛、基本对读者有参考价值，默认应保留并展开为卡片；目标是产出有实质内容的日报，不要整体留空——除非确实全是垃圾，否则至少保留明显达标的几条。"
            )

        # 调用 AI（生成级重试：应对空生成 / 解析失败 / 结构不完整）
        content = None
        for gen in range(1, GEN_RETRIES + 1):
            try:
                content = _call_ai(base_urls, api_key, model, system_prompt, user_prompt, stream=True)
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
            # 骨架拦截：纯标题、无字段实质内容的日报必须重生成，
            # 否则渲染后只剩标题（空字段被隐藏），用户体验极差且浪费额度。
            if _is_skeleton(report):
                t, z, inst = _analyze_richness(report)
                LOG.error("生成内容疑似骨架（items=%d，零字段=%d，字段实例=%d），第 %d/%d 次生成将重生成",
                          t, z, inst, gen, GEN_RETRIES)
                if gen < GEN_RETRIES:
                    time.sleep(BACKOFF_BASE)
                    continue
                # 首跑（无累积）仍拒绝空壳；已有累积则跳过该批次，保留旧内容
                if acc_total == 0:
                    raise SystemExit("skeleton report rejected: only titles, no field content")
                LOG.warning("新增批次疑似骨架，跳过该批次（保留已累积 %d 条）。", acc_total)
                report = None
                break
            # 行动性校验：剔除空心条目（名人动态/人事变动/纯融资/正确废话）
            report, hollow_removed = _actionable_check(report)
            if hollow_removed > 0 and sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES) == 0:
                LOG.error("行动性校验后所有条目被剔除，第 %d/%d 次生成将重生成", gen, GEN_RETRIES)
                if gen < GEN_RETRIES:
                    time.sleep(BACKOFF_BASE)
                    continue
                if acc_total == 0:
                    raise SystemExit("all items removed by actionable check")
                LOG.warning("新增批次全被剔除为空心，跳过该批次（保留已累积 %d 条）。", acc_total)
                report = None
                break
            # v4.3 L3：同玩法/同地域服务聚类去重（地域中性，仅同模块+同地域+同服务≥3才收口）
            report, cluster_removed = _cluster_dedup(report)
            if cluster_removed > 0:
                LOG.warning("【聚类去重】剔除 %d 条同玩法/同地域服务冗余", cluster_removed)
            # v4.4 L4：标题归一化去重（防 AI 生成阶段产出跨源重复条目 / 同文不同源）
            report, title_removed = _title_dedup_report(report)
            if title_removed > 0:
                LOG.warning("【标题去重】剔除 %d 条重复/高度相似条目", title_removed)
            total = sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES)
            if total == 0 and len(new_signals) > 0:
                LOG.warning("AI 返回 0 条但候选信号有 %d 条（疑似空生成），重生成（%d/%d）",
                            len(new_signals), gen, GEN_RETRIES)
                if gen < GEN_RETRIES:
                    time.sleep(BACKOFF_BASE)
                    continue
                LOG.warning("已达最大生成次数仍为 0 条。")
            break

        # 安全护栏（Fix B）：全新生成（acc_total==0）且候选非空，但 AI 连续返回 0 条模块
        # ——属模型偶发整体拒收（如首次清洁 run 出现的 4/4 空生成）。
        # 绝不发布空文章去覆盖线上真实内容：跳过本次发布，保留线上原状。
        final_total = sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES) \
            if isinstance(report, dict) else 0
        if final_total == 0 and len(new_signals) > 0 and acc_total == 0:
            LOG.error("生成返回 0 条但候选有 %d 条（疑似模型整体拒收），为避免发布空文章覆盖真实内容，跳过本次发布。",
                      len(new_signals))
            _emit_changed(False)
            return

        if report is None:
            # 骨架/空批次被跳过：无新增有效内容，保留已累积内容不发布
            _emit_changed(False)
            LOG.info("本次无有效新增内容，跳过发布（已累积 %d 条保留）。", acc_total)
            return

        # 合并到当日累积（旧内容保留 + 新内容去重追加）
        merged = C.merge_reports(accumulated, report)
        # 强制分波 / 全天条数上限：首波 ≤30；末波无早波收录 ≤60；
        # 末波有早波收录 ≤ 已有+30（即本波新增 ≤30，全天 ≤60）
        if is_final:
            hard = 60 if acc_total == 0 else min(60, acc_total + 30)
        else:
            hard = 30
        merged["modules"] = C.cap_modules(merged["modules"], hard)
        merged["date"] = today
        merged["timezone"] = "Asia/Shanghai"
        report = merged

        # 末波补全 daily_summary（主生成常被截断在尾部，导致『每日总结』丢失 → 补全以保证完整日报）
        if is_final:
            _ensure_daily_summary(report, base_urls, api_key, model, today)

    # 写回累积状态 & 当日报告
    C.save_json(daily_state_path, report)
    out_path = os.path.join(DATA_DIR, f"report-{today}.json")
    C.save_json(out_path, report)
    # 打分地基（SHADOW）：仅计算并汇总质量分数分布，不拦截（避免误杀）；
    # 待 shadow 数据验证精确率≥80% 后再转主动拦截。详见 themes/sidehustle.json。
    try:
        score.shadow_score_report(report)
    except Exception as e:
        LOG.warning("打分地基 SHADOW 评估跳过（不影响发布）: %s", e)
    _emit_changed(True)
    total = sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES)
    LOG.info("日报累积更新：当日共 %d 条（新增 %d），写入 %s，总耗时 %.1fs",
             total, len(new_signals), out_path, time.time() - overall_t0)

    # —— 源管理系统：统计每源最终成卡数 + 记录本次运行成本（均非阻塞）——
    try:
        import source_manager as SM
        SM.record_contributions(report)
    except Exception as e:
        LOG.warning("源成卡统计失败（不影响主流程）: %s", e)
    try:
        out_chars = len(json.dumps(report, ensure_ascii=False))
        C.log_run_cost(chars_out=out_chars, tag="generate_report")
    except Exception:
        pass


if __name__ == "__main__":
    main()
