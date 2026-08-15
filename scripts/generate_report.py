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
MAX_OUTPUT_TOKENS = 12000   # 输出 token 上限（收敛输出，降低超时/截断概率；
                              # 探针实测 35 候选大 prompt 在 6000 处被截断导致 JSON 残缺，
                              # 提到 12000 留足 3 模块结构余量，流式回传下 524 风险仍可控）

# —— 分批筛选（避免大量候选被直接截断丢弃，提升内容丰富度）——
SCREEN_THRESHOLD = 60     # 候选超过此数才启用分批筛选；否则全量直送生成（省调用、保速度）
SCREEN_BATCH = 60         # 每批送入筛选的候选数（平衡单批覆盖度与调用次数）
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
        # 国内网关 ai.jinbufenzi.com 默认模型为 qwen3.6-35b-a3b（推理模型）：
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
        mods[key] = cleaned
    assert "daily_summary" in report, "缺少 daily_summary"
    ds = report["daily_summary"]
    assert isinstance(ds, dict), "daily_summary 不是对象"
    # 容错：AI 偶发漏字段不致命，补默认值即可，避免整跑崩溃
    ds.setdefault("methodology", "")
    ds.setdefault("evidence", [])
    return True


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


def main():
    overall_t0 = time.time()
    C.ensure_dirs()
    today = C.date_str()
    LOG.info("开始生成 %s 日报", today)

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
    # 模型默认值随网关自适应：国内网关 ai.jinbufenzi.com 默认 qwen3.6-35b-a3b；
    # 海外 Gemini 默认 gemini-flash-latest。用户可用 AI_MODEL/ai_model 显式覆盖。
    _default_model = "gemini-flash-latest"
    if "jinbufenzi" in _first_base:
        _default_model = "qwen3.6-35b-a3b"
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
    accumulated = C.load_json(daily_state_path, {})
    acc_modules = accumulated.get("modules", {}) if isinstance(accumulated, dict) else {}
    existing_keys = set()
    for mod in C.MODULES:
        for it in acc_modules.get(mod, []):
            existing_keys.add(C.item_dedup_key(it))

    # 本次新增（未被当日累积过的）信号
    new_signals = [s for s in signals if C.item_dedup_key(s) not in existing_keys]

    # 渲染器版本或强制覆盖 → 仅重渲染，不重新调 AI
    force_rerender = os.environ.get("DOCGEN_FORCE_RERENDER", "").strip() in ("1", "true", "True")

    acc_total = sum(len(acc_modules.get(m, [])) for m in C.MODULES)
    changed = bool(new_signals) or force_rerender

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

        # 为最终生成裁剪信号：保留元数据，content 截断到 400 字符，
        # 减少高延迟网关上下文压力，降低 400/截断概率。
        prompt_signals = _trim_signals_for_prompt(new_signals, max_content=400)
        user_prompt = (
            f"今天是 {today}（北京时间）。以下是已完成去重的当日【新增】信号（JSON），"
            f"请仅基于这些新增信号生成条目，勿重复已有内容：\n"
            f"```json\n{json.dumps(prompt_signals, ensure_ascii=False, indent=2)}\n```\n"
            f"请严格按上面 SKILL.md（v3.0 老创业人基底）的规则，输出 ai-sidehustle-report 日报 JSON。"
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
            total = sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES)
            if total == 0 and len(new_signals) > 0:
                LOG.warning("AI 返回 0 条但候选信号有 %d 条（疑似空生成），重生成（%d/%d）",
                            len(new_signals), gen, GEN_RETRIES)
                if gen < GEN_RETRIES:
                    time.sleep(BACKOFF_BASE)
                    continue
                LOG.warning("已达最大生成次数仍为 0 条。")
            break

        if report is None:
            # 骨架/空批次被跳过：无新增有效内容，保留已累积内容不发布
            _emit_changed(False)
            LOG.info("本次无有效新增内容，跳过发布（已累积 %d 条保留）。", acc_total)
            return

        # 合并到当日累积（旧内容保留 + 新内容去重追加）
        merged = C.merge_reports(accumulated, report)
        merged["date"] = today
        merged["timezone"] = "Asia/Shanghai"
        report = merged

    # 写回累积状态 & 当日报告
    C.save_json(daily_state_path, report)
    out_path = os.path.join(DATA_DIR, f"report-{today}.json")
    C.save_json(out_path, report)
    _emit_changed(True)
    total = sum(len(report.get("modules", {}).get(k, [])) for k in C.MODULES)
    LOG.info("日报累积更新：当日共 %d 条（新增 %d），写入 %s，总耗时 %.1fs",
             total, len(new_signals), out_path, time.time() - overall_t0)


if __name__ == "__main__":
    main()
