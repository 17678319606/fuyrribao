#!/usr/bin/env python3
"""步骤3：把日报 JSON 渲染成文章 HTML，经 WordPress REST API 直接发布（非草稿）。
密钥走环境变量（由 GitHub Secrets 注入），绝不硬编码。
排版：每个条目为卡片；字段用 H3（带「：」号）+ 独立段落呈现；来源/证据均为文字链。
"""
import os
import sys
import json
import html
import base64
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1f2329;--sub:#6b7280;--line:#eaecef;
--brand:#e8543f;--brand-soft:#fdeeeb;--blue:#2f6fed;--purple:#7c5cff;--amber:#d98a00;}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.75;}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 60px;}
header.top{border-bottom:3px solid var(--brand);padding-bottom:18px;}
.kicker{color:var(--brand);font-weight:700;letter-spacing:2px;font-size:13px;}
h1{font-size:30px;margin:6px 0 4px;font-weight:800;}
.date{color:var(--sub);font-size:14px;}
.lede{background:var(--brand-soft);border-radius:12px;padding:14px 16px;color:#7a2c20;font-size:15px;margin:18px 0 28px;}
.module{margin:36px 0;}
.m-head{display:flex;align-items:center;gap:10px;margin-bottom:16px;}
.m-tag{width:8px;height:24px;border-radius:4px;}
.m-title{font-size:21px;font-weight:800;margin:0;}
.m-count{color:var(--sub);font-size:13px;margin-left:auto;}
.m1 .m-tag{background:var(--brand);}.m1 .m-title{color:var(--brand);}
.m2 .m-tag{background:var(--blue);}.m2 .m-title{color:var(--blue);}
.m3 .m-tag{background:var(--purple);}.m3 .m-title{color:var(--purple);}
.item{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:18px;box-shadow:0 2px 10px rgba(17,24,39,.06);}
.it-title{font-size:20px;font-weight:800;color:var(--ink);margin:0 0 6px;line-height:1.4;}
.it-meta{font-size:13px;color:var(--sub);margin:0 0 4px;padding-bottom:12px;border-bottom:1px solid var(--line);}
.it-meta a{color:var(--blue);text-decoration:none;font-weight:600;}
.it-meta a:hover{text-decoration:underline;}
.field{font-size:15px;font-weight:700;margin:16px 0 6px;padding-left:10px;border-left:3px solid var(--brand);line-height:1.3;}
.field.m1{border-color:var(--brand);color:var(--brand);}
.field.m2{border-color:var(--blue);color:var(--blue);}
.field.m3{border-color:var(--purple);color:var(--purple);}
.field.per{border-color:var(--purple);color:#5b3fa0;}
.ftext{font-size:14.5px;color:var(--ink);margin:0 0 4px;line-height:1.85;}
.perspective{background:#f6f3ff;border-left:3px solid var(--purple);border-radius:0 10px 10px 0;padding:12px 14px;margin:0 0 4px;}
.perspective p{margin:0;font-size:14.5px;color:#3d2d6b;line-height:1.85;}
.summary{background:#fff;border:1px solid var(--line);border-left:4px solid var(--amber);border-radius:12px;padding:20px;}
.summary h2{margin:0 0 10px;font-size:20px;color:var(--amber);}
.meth{font-size:15px;line-height:1.85;}
.evidence{margin-top:12px;font-size:13px;color:var(--sub);line-height:1.9;}
.evidence a{color:var(--blue);text-decoration:none;font-weight:600;margin:0 2px;}
.evidence a:hover{text-decoration:underline;}
footer{margin-top:50px;text-align:center;color:var(--sub);font-size:12px;}
"""

MODULES = [
    ("project_opportunities", "项目机会库", "m1"),
    ("growth_operations", "增长运营", "m2"),
    ("views_insights", "观点心法", "m3"),
]
ITEM_FIELDS = [
    ("signal", "信号"),
    ("why_now", "为什么现在还能做"),
    ("how_to", "建议怎么做"),
    ("monetization", "变现说明"),
    ("replicable", "可复制性"),
    ("perspective", "副业视角解读"),
]


def _domain(u):
    try:
        return urlparse(u).netloc.replace("www.", "")
    except Exception:
        return u


def render_item(it, modcls):
    title = html.escape(it.get("title", ""))
    src_name = html.escape(it.get("source_name", ""))
    src_url = html.escape(it.get("source_url", ""))
    rows = ""
    for key, label in ITEM_FIELDS:
        val = it.get(key)
        if not val:
            continue
        if key == "perspective":
            rows += (f'<h3 class="field per">副业视角解读：</h3>\n'
                     f'<div class="perspective"><p>{html.escape(str(val))}</p></div>')
        else:
            rows += (f'<h3 class="field {modcls}">{label}：</h3>\n'
                     f'<p class="ftext">{html.escape(str(val))}</p>')
    return f'''<article class="item">
<h3 class="it-title">{title}</h3>
<div class="it-meta">来源：{src_name} · <a href="{src_url}" target="_blank" rel="noopener">阅读原文 →</a></div>
{rows}
</article>'''


def render(report):
    date = report.get("date", C.date_str())
    total = sum(len(report["modules"].get(k, [])) for k, _, _ in MODULES)
    body = f'''<header class="top">
<div class="kicker">AI 副业日报</div>
<h1>副业日报 · {html.escape(date)}</h1>
<div class="date">{html.escape(date)}（北京时间）· 自动生成</div>
</header>
<div class="lede">今日共筛出 {total} 条增量信号，按「项目机会库 / 增长运营 / 观点心法」分模块呈现。</div>'''
    for key, title, cls in MODULES:
        items = report["modules"].get(key, [])
        if not items:
            continue
        cards = "".join(render_item(it, cls) for it in items)
        body += f'''<section class="module {cls}">
<div class="m-head"><span class="m-tag"></span><h2 class="m-title">{title}</h2>
<span class="m-count">精选 {len(items)}</span></div>{cards}</section>'''
    ds = report.get("daily_summary", {})
    meth = html.escape(ds.get("methodology", ""))
    # 证据文字链：优先用条目里的来源名/标题，否则退化为域名
    url_map = {}
    for key, _, _ in MODULES:
        for it in report["modules"].get(key, []):
            u = it.get("source_url")
            if u:
                url_map[u] = (it.get("source_name", ""), it.get("title", ""))
    ev_parts = []
    for u in ds.get("evidence", []):
        sn, ti = url_map.get(u, ("", ""))
        label = ti or sn or _domain(u)
        ev_parts.append(
            f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(label)}</a>')
    ev = " · ".join(ev_parts)
    if meth:
        body += f'''<section class="module"><div class="summary">
<h2>📌 每日总结 · 今日可复用方法论</h2>
<div class="meth">{meth}</div>
<div class="evidence">证据：{ev}</div></div></section>'''
    return (f'<style>{CSS}</style>\n'
            f'<div class="wrap">{body}'
            f'<footer>本文由 GitHub Actions 自动生成并发布。</footer></div>')


def get_category_id(session, base, auth, name):
    import requests
    r = session.get(base + "/wp-json/wp/v2/categories", params={"search": name},
                    headers=auth, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data:
        return data[0]["id"]
    r = session.post(base + "/wp-json/wp/v2/categories", json={"name": name},
                     headers=auth, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def already_published(session, base, auth, title):
    import requests
    r = session.get(base + "/wp-json/wp/v2/posts",
                    params={"search": title, "status": "publish"},
                    headers=auth, timeout=30)
    r.raise_for_status()
    return any(title in p.get("title", {}).get("rendered", "") for p in r.json())


def main():
    C.ensure_dirs()
    today = C.date_str()
    report = C.load_json(os.path.join(C.DATA_DIR, f"report-{today}.json"), {})
    if not report:
        LOG.info("无日报数据，跳过发布。")
        return
    total = sum(len(report.get("modules", {}).get(k, [])) for k, _, _ in MODULES)
    ds = report.get("daily_summary", {})
    if total == 0 and not ds.get("methodology"):
        LOG.info("今日无实质内容，跳过发布（不发布空文章）。")
        return

    content = render(report)
    title = f"副业日报 · {today}"

    wp_url = os.environ.get("WP_URL", "https://dajiayouxuan.com").rstrip("/")
    user = os.environ.get("WP_USER", "tougao")
    app_pw = os.environ.get("WP_APP_PASSWORD", "")
    cat_name = os.environ.get("WP_CATEGORY_NAME", "日报")
    base = wp_url

    import requests
    session = requests.Session()
    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{app_pw}".encode()).decode(), "Content-Type": "application/json"}

    if already_published(session, base, auth, title):
        LOG.info("今日文章已存在，跳过发布（防重复）。")
        return

    try:
        cat_id = get_category_id(session, base, auth, cat_name)
    except Exception as e:
        LOG.warning("类目解析失败，退回不指定类目: %s", e)
        cat_id = None

    payload = {"title": title, "content": content, "status": "publish"}
    if cat_id:
        payload["categories"] = [cat_id]
    r = session.post(base + "/wp-json/wp/v2/posts", json=payload, headers=auth, timeout=60)
    r.raise_for_status()
    link = r.json().get("link", "")
    LOG.info("✅ 已发布: %s", link)
    print("PUBLISHED_URL=" + link)


if __name__ == "__main__":
    main()
