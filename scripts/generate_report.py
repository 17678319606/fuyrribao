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
    """流式空响应 / 整体超时且内容无法解析：放弃当前端点，转下一个端点或非流式兜底。"""
    pass


SKILL_FILE = C.SKILL_FILE
DATA_DIR = C.DATA_DIR

RETRY_PER_ENDPOINT = 2
BACKOFF_BASE = 8
REQ_TIMEOUT = (15, 90)      # connect 15s, read 90s
GEN_RETRIES = 4
STREAM_MAX_SECONDS = 120    # bounded stream wall-clock limit
MAX_INPUT_SIGNALS = 50
MAX_OUTPUT_TOKENS = 8000

SCREEN_THRESHOLD = 60
SCREEN_BATCH = 60
SCREEN_KEEP_PER_BATCH = 10
SCREEN_FINAL_CAP = 35


def _req_timeout():
    raw = os.environ.get("AI_REQUEST_TIMEOUT", "").strip()
    if raw:
        try:
            return (15, min(int(raw), 90))
        except ValueError:
            pass
    return REQ_TIMEOUT


def _force_non_stream():
    raw = os.environ.get("AI_FORCE_NON_STREAM", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _trim_signals_for_prompt(signals, max_content=400):
    out = []
    for s in signals:
        c = dict(s)
        content = c.get("content") or ""
        if len(content) > max_content:
            c["content"] = content[:max_content].rstrip() + "…（已截断）"
        out.append(c)
    return out


def _is_valid_proxy(p):
    try:
        u = urlparse(p)
    except Exception:
        return False
    if u.scheme not in ("http", "https", "socks5", "socks4"):
        return False
    host = u.hostname or ""
    if not host or not host.isascii():
        return False
    low = p.lower()
    return not any(k in low for k in ("代理", "placeholder", "example", "xxxx", "your_", "ip:端口", "ip:port"))


def _parse_proxies():
    raw = os.environ.get("AI_PROXY_POOL", "").strip()
    if not raw:
        return []
    out = []
    for p in re.split(r"[;,\n]", raw):
        p = p.strip()
        if p and _is_valid_proxy(p):
            out.append(p)
    return out


def _parse_base_urls():
    primary = os.environ.get("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").strip()
    pool_raw = os.environ.get("AI_BASE_URL_POOL", "").strip()
    seen, out = set(), []
    for u in [primary] + re.split(r"[;,\n]", pool_raw):
        u = u.strip().rstrip("/")
        low = u.lower()
        if not u or not u.startswith(("http://", "https://")):
            continue
        if any(k in low for k in ("placeholder", "example", "xxxx", "your_", "example.com")):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out or ["https://generativelanguage.googleapis.com/v1beta"]


def _candidate_endpoints():
    cands = [None] + _parse_proxies()
    random.shuffle(cands)
    return cands


def _is_retryable_http(status):
    return status == 0 or status == 429 or (500 <= status < 600)


def _retry_after_seconds(resp):
    if not resp or not getattr(resp, "headers", None):
        return 0
    try:
        return min(max(int(resp.headers.get("Retry-After", "0")), 0), 60)
    except ValueError:
        return 0


def _doh_resolve(host):
    import urllib.request
    providers = [
        ("Google", "https://dns.google/resolve?name=%s&type=A" % host, {"Accept": "application/dns-json"}),
        ("Cloudflare", "https://cloudflare-dns.com/dns-query?name=%s&type=A" % host, {"Accept": "application/dns-json"}),
    ]
    ips = []
    for name, url, hdr in providers:
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read())
            ips.extend(a["data"] for a in d.get("Answer", []) if a.get("type") == 1)
        except Exception as e:
            LOG.warning("DoH(%s) 解析 %s 失败: %s", name, host, e)
    return list(dict.fromkeys(ips))


_DNS_PATCH_HOSTS = {}


def _install_dns_patch(host):
    if "jinbufenzi" not in host or _DNS_PATCH_HOSTS.get(host):
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


def _is_gemini_native(base_url):
    low = (base_url or "").lower()
    return "generativelanguage.googleapis.com" in low and "/openai/" not in low


def _call_gemini_native(base_url, api_key, model, system_prompt, user_prompt, stream=True):
    start = time.time()
    base_url = base_url.rstrip("/")
    model_id = model.split("/")[-1] if "/" in model else model
    action = "streamGenerateContent" if stream else "generateContent"
    url = f"{base_url}/models/{model_id}:{action}?key={api_key}"
    if stream:
        url += "&alt=sse"
    full_prompt = f"{system_prompt}\n\n{user_prompt}".strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": MAX_OUTPUT_TOKENS},
    }
    resp = requests.post(url, headers={"Content-Type": "application/json"},
                         json=payload, timeout=_req_timeout(), stream=stream)
    resp.raise_for_status()
    if not stream:
        text = ""
        for cand in resp.json().get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        if not text:
            raise RuntimeError("Gemini 原生非流式响应为空")
        return text

    content = ""
    deadline = time.time() + STREAM_MAX_SECONDS
    for raw_line in resp.iter_lines(decode_unicode=False):
        if time.time() > deadline:
            break
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        for cand in obj.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                content += part.get("text", "")
    if not content:
        raise RuntimeError("Gemini 原生流式响应为空")
    LOG.info("Gemini 原生请求成功（model=%s，耗时 %.1fs，约 %d 字）", model_id, time.time() - start, len(content))
    return content


def _call_ai(base_urls, api_key, model, system_prompt, user_prompt, stream=True):
    if isinstance(base_urls, str):
        base_urls = [base_urls]
    if _force_non_stream():
        stream = False
    last_err = None
    timeout = _req_timeout()
    strip_thinking = False
    endpoints = []
    seen = set()
    fb_url = os.environ.get("AI_FALLBACK_URL", "").strip().rstrip("/")
    fb_key = os.environ.get("AI_FALLBACK_KEY", "").strip()
    fb_model = os.environ.get("AI_FALLBACK_MODEL", "").strip() or model
    allow_fallback = os.environ.get("AI_ENABLE_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
    if api_key and not allow_fallback:
        fb_url = ""
    for u in base_urls:
        u = (u or "").strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            endpoints.append((u, api_key, model))
    if fb_url and fb_url not in seen:
        endpoints.append((fb_url, fb_key, fb_model))

    for base_url, endpoint_key, endpoint_model in endpoints:
        if _is_gemini_native(base_url):
            models = []
            for candidate in [endpoint_model, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-flash-latest"]:
                if candidate and candidate not in models:
                    models.append(candidate)
            for native_model in models:
                for attempt in range(2):
                    try:
                        return _call_gemini_native(base_url, endpoint_key, native_model,
                                                   system_prompt, user_prompt, stream)
                    except Exception as e:
                        last_err = e
                        LOG.warning("Gemini 原生调用失败（model=%s，第%d/2次）：%s", native_model, attempt + 1, e)
                        if attempt == 0:
                            time.sleep(3)
            continue

        url = base_url + "/chat/completions"
        host = urlparse(base_url).netloc
        _install_dns_patch(host)
        headers = {"Authorization": f"Bearer {endpoint_key}", "Content-Type": "application/json"}
        payload = {"model": endpoint_model, "temperature": 0.5, "max_tokens": MAX_OUTPUT_TOKENS,
                   "stream": stream, "messages": [{"role": "system", "content": system_prompt},
                                                    {"role": "user", "content": user_prompt}]}
        if "jinbufenzi" in host:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        for proxy in _candidate_endpoints():
            proxies = {"http": proxy, "https": proxy} if proxy else None
            for attempt in range(1, RETRY_PER_ENDPOINT + 1):
                payload_now = dict(payload)
                if strip_thinking:
                    payload_now.pop("chat_template_kwargs", None)
                try:
                    resp = requests.post(url, headers=headers, json=payload_now, proxies=proxies,
                                         timeout=timeout, stream=stream)
                    resp.raise_for_status()
                    if not stream:
                        msg = (resp.json().get("choices", [{}])[0].get("message", {}).get("content", ""))
                        if msg:
                            return msg
                        raise RuntimeError("non-stream response empty")
                    content = ""
                    deadline = time.time() + STREAM_MAX_SECONDS
                    for raw_line in resp.iter_lines(decode_unicode=False):
                        if time.time() > deadline:
                            break
                        line = raw_line.decode("utf-8", "replace").strip() if raw_line else ""
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if choices:
                            content += (choices[0].get("delta") or {}).get("content") or ""
                    if content:
                        return content
                    raise _AbandonEndpoint("流式响应为空")
                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response is not None else 0
                    if status == 400 and "chat_template_kwargs" in payload and not strip_thinking:
                        strip_thinking = True
                        continue
                    last_err = e
                    if not _is_retryable_http(status):
                        break
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                        requests.exceptions.InvalidURL, socket.gaierror, _AbandonEndpoint) as e:
                    last_err = e
                if attempt < RETRY_PER_ENDPOINT:
                    time.sleep(min(BACKOFF_BASE * attempt, 15))
    raise last_err or RuntimeError("AI endpoints exhausted")


def main():
    # Preserve the existing report-generation implementation below this point.
    raise SystemExit("patched generation core requires original report helpers")


if __name__ == "__main__":
    main()
