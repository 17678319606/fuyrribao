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

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

SKILL_FILE = C.SKILL_FILE
DATA_DIR = C.DATA_DIR

RETRY_PER_ENDPOINT = 4      # 每个候选端点（直连 / 代理）的最大尝试次数
BACKOFF_BASE = 3            # 退避基数（秒），呈指数增长：3 / 6 / 12 / 24s
REQ_TIMEOUT = (15, 150)     # (connect, read) 超时，read 放宽以容纳 16k tokens 输出


def _parse_proxies():
    """从 AI_PROXY_POOL 解析代理列表，过滤空值。"""
    raw = os.environ.get("AI_PROXY_POOL", "").strip()
    if not raw:
        return []
    out = []
    for p in re.split(r"[;,\n]", raw):
        p = p.strip()
        if p:
            out.append(p)
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


def _call_ai(base_url, api_key, model, system_prompt, user_prompt):
    """调用 /chat/completions，带端点轮换 + 重试。返回模型原始 content 字符串。"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.5,
        "max_tokens": 16000,
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
                LOG.info("AI 请求 [端点 %d/%d=%s] 第 %d/%d 次尝试",
                         ci + 1, len(cands), label, attempt, RETRY_PER_ENDPOINT)
                resp = requests.post(
                    url, headers=headers, json=payload,
                    proxies=proxies, timeout=REQ_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                LOG.info("AI 请求成功（端点=%s）", label)
                return content
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
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
    raise RuntimeError(f"所有候选端点（{len(cands)}）均失败，最后错误：{last_err}")


def _extract_json(text):
    """从模型输出里抠出 JSON（兼容 ```json 围栏 / 前后多余文字 / 中文引号）。"""
    if not text:
        return None
    s = text.strip()
    if "```" in s:
        m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
        if m:
            s = m.group(1).strip()
    a = s.find("{")
    b = s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    for cand in (s, s.replace("“", "\"").replace("”", "\""),
                 s.replace("‘", "'").replace("’", "'")):
        try:
            return json.loads(cand)
        except Exception:
            continue
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

    content = _call_ai(base_url, api_key, model, system_prompt, user_prompt)

    # 4) 解析 + 校验
    report = _extract_json(content)
    if report is None:
        LOG.error("AI 返回无法解析为 JSON，原始前 500 字：%s", content[:500])
        raise SystemExit("invalid AI json")
    _validate(report)
    report["date"] = today
    report["timezone"] = "Asia/Shanghai"

    out_path = os.path.join(DATA_DIR, f"report-{today}.json")
    C.save_json(out_path, report)
    total = sum(len(report["modules"].get(k, [])) for k in
                ("project_opportunities", "growth_operations", "views_insights"))
    LOG.info("日报已生成：3 个模块共 %d 条，写入 %s", total, out_path)


if __name__ == "__main__":
    main()
