#!/usr/bin/env python3
"""周期主题合集（Item2）：从条目持久化 store 取近 N 天内容，由 AI 合成『唯一』主题合集。

设计约束（防 SEO 自伤 / 小机友好）：
- 合集是 AI 原创评述与归纳，不照搬条目原文；仅穿插短引用(≤1句)并链回原文/日报，
  形成「枢纽页 → 源页」的正向内链，而非重复内容。
- 周更（低频），单次发布；条目不足阈值则跳过，绝不发薄页。
- 不把合集本身写回 items_store，避免自我递归与薄内容累积。
"""
import os
import sys
import json
import time
import base64
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

WINDOW_DAYS = int(os.environ.get("ROUNDUP_WINDOW_DAYS", "7"))
MIN_ITEMS = int(os.environ.get("ROUNDUP_MIN_ITEMS", "6"))


def _esc(s):
    import html as html_lib
    return html_lib.escape(str(s)) if s else ""


def _load_store(days):
    path = os.path.join(C.STATE_DIR, "items_store.jsonl")
    if not os.path.exists(path):
        return []
    cutoff = C.days_ago_iso(days)[:10]
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("date", "") >= cutoff:
                out.append(e)
    return out


def _ai_config():
    base = (os.environ.get("ai_base_url", "") or os.environ.get("AI_BASE_URL", "")
            or "https://ai.jinbufenzi.com/v1").rstrip("/")
    key = (os.environ.get("AI_API_KEY", "") or os.environ.get("ai_api_key", "")
           or os.environ.get("AI_SIDEHUSTLE_API_KEY", "")).strip()
    model = (os.environ.get("AI_MODEL", "") or os.environ.get("ai_model", "")
             or "auto").strip()
    if not key:
        raise RuntimeError("AI_API_KEY/ai_api_key 未设置")
    return base, key, model


def _call_ai(prompt):
    import requests
    base, key, model = _ai_config()
    url = base + "/chat/completions"
    auth = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "你是资深中文副业内容编辑，输出严格 JSON，不要任何解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    last = None
    for i in range(1, 4):
        try:
            r = requests.post(url, headers=auth, json=body, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = 5 * i
                LOG.warning("AI 返回 %s，%ds 后重试", r.status_code, wait)
                time.sleep(wait)
                last = RuntimeError("ai %s" % r.status_code)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            time.sleep(5 * i)
    raise last or RuntimeError("ai call failed")


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI 未返回 JSON")
    return json.loads(m.group(0))


def _render_roundup(theme, html, today):
    S = (
        "max-width:760px;width:100%;margin:0 auto!important;"
        "padding:clamp(18px,5vw,36px) clamp(12px,4vw,24px) clamp(36px,8vw,64px)!important;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif!important;"
        "line-height:1.85!important;color:#2b2b2b!important;background:#faf9f7!important;"
        "box-sizing:border-box!important;"
    )
    H1 = "font-size:clamp(22px,6vw,30px);font-weight:900;color:#1f1f1f;margin:6px 0 4px;"
    DATE = "color:#8a8a8a;font-size:14px;"
    WRAP = ("background:#fff;border:1px solid #e6e3dd;border-left:3px solid #2f6b5e;"
            "border-radius:12px;padding:14px 18px;margin:18px 0 26px;font-size:15px;color:#4a4a4a;")
    return (
        f'<div style="{S}">'
        f'<div style="text-align:center;padding-bottom:18px;border-bottom:2px solid #e6e3dd;margin-bottom:24px;">'
        f'<div style="color:#2f6b5e;font-weight:700;font-size:12px;letter-spacing:2px;">副业主题合集</div>'
        f'<h1 style="{H1}">{_esc(theme)}</h1>'
        f'<div style="{DATE}">{today}（北京时间）· 自动生成</div>'
        f'</div>'
        f'<div style="{WRAP}">以下为近期副业日报内容的主题化梳理与原创评述，附原文链接，便于深度阅读。</div>'
        f'{html}'
        f'</div>'
    )


def _publish(base, auth, title, content, cat_name):
    import requests
    cat_id = None
    try:
        r = requests.get(base + "/wp-json/wp/v2/categories",
                         params={"search": cat_name}, headers=auth, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data:
            cat_id = data[0]["id"]
        else:
            r = requests.post(base + "/wp-json/wp/v2/categories",
                              json={"name": cat_name}, headers=auth, timeout=30)
            r.raise_for_status()
            cat_id = r.json()["id"]
    except Exception as e:
        LOG.warning("合集类目解析失败: %s", e)
        cat_id = None
    payload = {"title": title, "content": content, "status": "publish"}
    if cat_id:
        payload["categories"] = [cat_id]
    s = requests.Session()
    r = s.post(base + "/wp-json/wp/v2/posts", headers=auth, json=payload, timeout=90)
    r.raise_for_status()
    return r.json().get("id"), r.json().get("link", "")


def main():
    C.ensure_dirs()
    if os.environ.get("FUYR_DISABLE_PUBLISH", "").strip() in ("1", "true", "True", "yes"):
        LOG.info("FUYR_DISABLE_PUBLISH=1：跳过合集发布。")
        return
    today = C.date_str()
    items = _load_store(WINDOW_DAYS)
    if len(items) < MIN_ITEMS:
        LOG.info("近 %d 天条目仅 %d 条（<%d），跳过本期合集，避免薄内容。",
                 WINDOW_DAYS, len(items), MIN_ITEMS)
        return

    # 去重（按条目去重键），构造给 AI 的精简清单
    seen = set()
    cleaned = []
    for e in items:
        k = e.get("source_url") or e.get("title")
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(e)
    items = cleaned

    brief = [{
        "date": e.get("date", ""),
        "module": e.get("module", ""),
        "title": e.get("title", ""),
        "source_name": e.get("source_name", ""),
        "source_url": e.get("source_url", ""),
        "report_link": e.get("report_link", ""),
        "summary": e.get("summary", ""),
    } for e in items]
    brief_json = json.dumps(brief, ensure_ascii=False)

    prompt = (
        "你是资深中文副业内容编辑。下面是近 %d 天『副业日报』聚合的条目清单（JSON，"
        "每条含 date/module/title/source_name/source_url/report_link/summary）。\n\n"
        "任务：从中提炼一个**连贯主题**，写一篇约 700-1000 字的中文『主题合集』文章。\n"
        "硬性要求：\n"
        "1. 必须是原创评述与归纳，不得照搬条目原文；\n"
        "2. 文中穿插 2-4 处简短引用（每处不超过一句话），用 Markdown 链接 [标题](source_url) 标注来源；\n"
        "3. 末尾加『相关日报』小节，用 Markdown 链接 [日期](report_link) 列出涉及的几期日报；\n"
        "4. 仅输出 JSON，结构：{\"theme\": \"主题名(≤12字)\", \"html\": \"文章 HTML（用 <h2>/<h3>/<p>/<ul>/<li> 等基础标签，不要 style 属性）\"}。\n\n"
        "条目清单：\n%s"
    ) % (WINDOW_DAYS, brief_json)

    try:
        raw = _call_ai(prompt)
        data = _extract_json(raw)
        theme = (data.get("theme") or "副业主题合集").strip()
        html = (data.get("html") or "").strip()
    except Exception as e:
        LOG.error("AI 合成合集失败，跳过发布：%s", e)
        return

    if len(html) < 400:
        LOG.warning("合集 HTML 过短(%d 字)，疑似 AI 退化，跳过发布避免薄内容。", len(html))
        return

    content = _render_roundup(theme, html, today)
    wp_url = os.environ.get("WP_URL", "https://dajiayouxuan.com").rstrip("/")
    user = os.environ.get("WP_USER", "tougao")
    app_pw = os.environ.get("WP_APP_PASSWORD", "")
    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{app_pw}".encode()).decode(), "Content-Type": "application/json"}
    cat_name = os.environ.get("WP_ROUNDUP_CATEGORY", "主题合集")
    title = f"副业主题合集 · {theme} · {today}"
    try:
        pid, link = _publish(wp_url, auth, title, content, cat_name)
        LOG.info("已发布主题合集(post=%s): %s", pid, link)
        print("ROUNDUP_URL=" + link)
    except Exception as e:
        LOG.error("合集发布失败：%s", e)
        raise SystemExit("roundup publish failed")


if __name__ == "__main__":
    main()
