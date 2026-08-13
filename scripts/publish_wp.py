#!/usr/bin/env python3
"""步骤3：把日报 JSON 渲染成文章 HTML，经 WordPress REST API 直接发布（非草稿）。
密钥走环境变量（由 GitHub Secrets 注入），绝不硬编码。

v2 排版规范（基于创业画布）：
- 每个条目为独立卡片，带阴影和圆角
- 字段按创业画布 9 维度呈现：信号/趋势 → 目标客户 → 价值主张 → 建议怎么做/MVP
  → 获客渠道 → 变现说明与数据表现 → 启动成本 → 可复制性/壁垒 → 副业视角
- 每个字段用 H3（带「：」号）+ 独立段落；**留空的字段不展示标题也不占位**
- 来源/证据均为文字链（标题或来源名，不是裸 URL）
- 副业视角单独用紫色引用块突出
- 内联完整 CSS，确保 WordPress 主题不覆盖样式
- 响应式：移动端（≤480px）自动调整字号/间距/内边距，杜绝溢出
"""
import os
import sys
import json
import html as html_lib
import base64
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

# ── 完整内联 CSS（高优先级选择器 + !important 防止 WP 主题覆盖）──
CSS = """
.shr-wrap{max-width:780px;margin:0 auto;padding:28px 18px 56px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
  line-height:1.8;color:#1f2329;background:transparent;box-sizing:border-box;}
.shr-wrap *,.shr-wrap *::before,.shr-wrap *::after{box-sizing:border-box;}

/* ── 头部 ── */
.shr-header{border-bottom:3px solid #e8543f;padding-bottom:16px;margin-bottom:24px;}
.shr-kicker{color:#e8543f;font-weight:700;letter-spacing:2px;font-size:12px;text-transform:uppercase;}
.shr-h1{font-size:28px;margin:6px 0 2px;font-weight:900;line-height:1.3;color:#1f2329;}
.shr-date{color:#6b7280;font-size:13px;}
.shr-lede{background:#fef3ef;border-left:4px solid #e8543f;border-radius:0 10px 10px 0;
  padding:14px 18px;font-size:15px;color:#7a2c20;margin:20px 0 28px;line-height:1.75;}

/* ── 模块标题 ── */
.shr-mod{margin:40px 0 0;}
.shr-mhead{display:flex;align-items:center;gap:10px;margin-bottom:18px;}
.shr-mtag{width:8px;height:24px;border-radius:4px;flex-shrink:0;}
.shr-mtitle{font-size:22px;font-weight:800;margin:0;line-height:1.3;letter-spacing:-.2px;}
.shr-mcount{color:#6b7280;font-size:12px;margin-left:auto;flex-shrink:0;}
.shr-mod.m1 .shr-mtag{background:#e8543f;}.shr-mod.m1 .shr-mtitle{color:#e8543f;}
.shr-mod.m2 .shr-mtag{background:#2f6fed;}.shr-mod.m2 .shr-mtitle{color:#2f6fed;}
.shr-mod.m3 .shr-mtag{background:#7c5cff;}.shr-mod.m3 .shr-mtitle{color:#7c5cff;}

/* ── 卡片 ── */
.shr-card{background:#fff;border:1px solid #eaecef;border-radius:14px;
  padding:24px 26px;margin-bottom:18px;
  box-shadow:0 2px 12px rgba(17,24,39,.06);
  overflow:hidden;word-wrap:break-word;overflow-wrap:break-word;
  transition:box-shadow .2s ease,transform .2s ease;}
.shr-card:hover{box-shadow:0 4px 18px rgba(17,24,39,.1);transform:translateY(-1px);}
.shr-card:last-child{margin-bottom:0;}

.shr-it-title{font-size:21px;font-weight:800;color:#1a1a2e;margin:0 0 8px;line-height:1.4;
  letter-spacing:-.2px;padding-bottom:0!important;}
.shr-it-meta{font-size:12.5px;color:#6b7280;margin:0 0 14px;
  padding-bottom:12px!important;border-bottom:1px solid #eaecef;line-height:1.5;}
.shr-it-meta a{color:#2f6fed;text-decoration:none;font-weight:600;}
.shr-it-meta a:hover{text-decoration:underline;}

/* ── 画布字段 H3 + 正文 ── */
.shr-field{font-size:16px;font-weight:700;margin:18px 0 6px!important;
  padding-left:12px!important;border-left:4px solid #e8543f;
  line-height:1.45!important;display:block;letter-spacing:-.1px;}
.shr-field.m1{border-color:#e8543f!important;color:#d63d28!important;}
.shr-field.m2{border-color:#2f6fed!important;color:#2257d6!important;}
.shr-field.m3{border-color:#7c5cff!important;color:#6b46e0!important;}
.shr-field.per{border-color:#7c5cff!important;color:#5b3fa0!important;}

.shr-ftext{font-size:15px;color:#1f2329;margin:0 0 4px!important;line-height:1.9!important;
  padding-left:12px!important;}

/* ── 副业视角引用块 ── */
.shr-persp{background:#f6f3ff;border-left:4px solid #7c5cff;
  border-radius:0 10px 10px 0;padding:14px 18px;margin:16px 0 4px!important;}
.shr-persp p{margin:0!important;font-size:15px;color:#3d2d6b;line-height:1.9!important;}

/* ── 每日总结 ── */
.shr-summary{background:#fffbf0;border:1px solid #eaecef;
  border-left:4px solid #d98a00;border-radius:12px;padding:20px 22px;margin-top:40px;}
.shr-summary h2{margin:0 0 14px!important;font-size:20px!important;color:#d98a00!important;
  font-weight:800!important;line-height:1.3!important;padding:0!important;letter-spacing:-.2px;}
.shr-meth{font-size:15px;line-height:1.9!important;color:#1f2329;margin:0 0 10px!important;}
.shr-ev{margin-top:10px;font-size:12.5px;color:#6b7280;line-height:1.9!important;}
.shr-ev a{color:#2f6fed;text-decoration:none;font-weight:600;margin:0 3px;}
.shr-ev a:hover{text-decoration:underline;}

/* ── 页脚 ── */
.shr-footer{margin-top:48px;text-align:center;color:#999;font-size:11.5px;
  padding-top:20px;border-top:1px solid #eaecef;}

/* ── 响应式：移动端 ≤480px ── */
@media screen and (max-width:480px){
  .shr-wrap{padding:16px 12px 36px!important;}
  .shr-h1{font-size:23px!important;}
  .shr-mtitle{font-size:18px!important;}
  .shr-card{padding:18px 18px!important;border-radius:10px!important;}
  .shr-it-title{font-size:18.5px!important;}
  .shr-field{font-size:14.5px!important;margin:14px 0 5px!important;padding-left:10px!important;}
  .shr-ftext{font-size:14px!important;padding-left:10px!important;}
  .shr-persp{padding:12px 14px!important;}
  .shr-persp p{font-size:14px!important;}
  .shr-summary{padding:16px!important;}
  .shr-lede{padding:12px 14px!important;font-size:14px!important;}
}

/* ── 防止图片/表格溢出 ── */
.shr-card img{max-width:100%!important;height:auto!important;border-radius:6px;}
.shr-card table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;}
.shr-card pre{white-space:pre-wrap;word-break:break-all;font-size:13px!important;
  background:#f7f8fa;padding:12px;border-radius:8px;overflow-x:auto;}
"""

# ── 模块定义 ──
MODULES = [
    ("project_opportunities", "项目机会库", "m1"),
    ("growth_operations",     "增长运营",   "m2"),
    ("views_insights",        "观点心法",   "m3"),
]

# ── 创业画布字段定义（顺序=显示顺序）──
# (json_key, display_label)
CANVAS_FIELDS = [
    ("signal",              "信号 / 趋势"),
    ("target_customer",     "目标客户"),
    ("value_proposition",   "价值主张"),
    ("how_to_mvp",          "建议怎么做 / MVP"),
    ("acquisition_channel", "获客渠道"),
    ("monetization",        "变现说明与数据表现"),
    ("startup_cost",        "启动成本"),
    ("replicability",       "可复制性 / 壁垒"),
    ("perspective",         "副业视角"),
]


def _domain(u):
    """从 URL 提取域名（去 www.）。"""
    try:
        return urlparse(u).netloc.replace("www.", "")
    except Exception:
        return u


def _esc(s):
    """HTML 转义。"""
    return html_lib.escape(str(s)) if s else ""


def render_item(it, modcls):
    """渲染单条卡片。空字段不展示（标题+内容都隐藏）。"""
    title = _esc(it.get("title", ""))
    src_name = _esc(it.get("source_name", ""))
    src_url = _esc(it.get("source_url", ""))

    rows = ""
    for key, label in CANVAS_FIELDS:
        val = it.get(key)
        if not val or not str(val).strip():
            continue  # 空字段：跳过，不输出标题也不占位

        val_str = str(val).strip()
        if key == "perspective":
            # 副业视角：紫色引用块
            rows += (
                f'<h3 class="field per shr-field per">副业视角：</h3>\n'
                f'<div class="perspective shr-persp"><p>{_esc(val_str)}</p></div>\n'
            )
        else:
            rows += (
                f'<h3 class="field {modcls} shr-field {modcls}">{_esc(label)}：</h3>\n'
                f'<p class="ftext shr-ftext">{_esc(val_str)}</p>\n'
            )

    return (
        f'<article class="card shr-card">\n'
        f'<h3 class="it-title shr-it-title">{title}</h3>\n'
        f'<div class="it-meta shr-it-meta">'
        f'来源：{src_name} · '
        f'<a href="{src_url}" target="_blank" rel="noopener noreferrer">阅读原文 →</a>'
        f'</div>\n'
        f'{rows}'
        f'</article>'
    )


def render(report):
    """将完整 report JSON 渲染为带内联样式的 HTML 片段。"""
    date = report.get("date", C.date_str())
    total = sum(len(report["modules"].get(k, [])) for k, _, _ in MODULES)

    body = (
        f'<header class="top shr-header">\n'
        f'<div class="kicker shr-kicker">AI 副业日报</div>\n'
        f'<h1 class="shr-h1">副业日报 · {_esc(date)}</h1>\n'
        f'<div class="date shr-date">{_esc(date)}（北京时间）· 自动生成</div>\n'
        f'</header>\n'
        f'<div class="lede shr-lede">今日共筛出 <strong>{total}</strong> 条增量信号，'
        f'按「项目机会库 / 增长运营 / 观点心法」分模块呈现。</div>\n'
    )

    for key, mtitle, cls in MODULES:
        items = report["modules"].get(key, [])
        if not items:
            continue
        cards = "".join(render_item(it, cls) for it in items)
        body += (
            f'<section class="module shr-mod {cls}">\n'
            f'<div class="mhead shr-mhead">'
            f'<span class="mtag shr-mtag"></span>'
            f'<h2 class="mtitle shr-mtitle">{mtitle}</h2>'
            f'<span class="mcount shr-mcount">精选 {len(items)}</span>'
            f'</div>\n'
            f'{cards}\n'
            f'</section>\n'
        )

    # ── 每日总结（含方法论）──
    ds = report.get("daily_summary", {})
    meth = ds.get("methodology", "")
    if meth:
        # 证据文字链：优先用条目里的标题/来源名，否则退化为域名
        url_map = {}
        for k, _, _ in MODULES:
            for it in report["modules"].get(k, []):
                u = it.get("source_url")
                if u:
                    url_map[u] = (it.get("source_name", ""), it.get("title", ""))
        ev_parts = []
        for u in ds.get("evidence", []):
            sn, ti = url_map.get(u, ("", ""))
            label = ti or sn or _domain(u)
            ev_parts.append(
                f'<a href="{_esc(u)}" target="_blank" rel="noopener noreferrer">'
                f'{_esc(label)}</a>')
        ev_html = " · ".join(ev_parts)

        body += (
            f'<section class="module"><div class="summary shr-summary">\n'
            f'<h2>每日总结 · 可复用方法论</h2>\n'
            f'<div class="meth shr-meth">{_esc(meth)}</div>\n'
            f'<div class="evidence shr-ev">证据：{ev_html}</div>\n'
            f'</div></section>\n'
        )

    return (
        f'<style>{CSS}</style>\n'
        f'<div class="wrap shr-wrap">\n'
        f'{body}\n'
        f'<footer class="shr-footer">本文由 GitHub Actions 自动生成并发布。</footer>\n'
        f'</div>'
    )


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


def find_existing_post(session, base, auth, title):
    """返回与今日标题匹配的已发布文章 id（无则 None），供防重复/强制更新判断。"""
    import requests
    r = session.get(base + "/wp-json/wp/v2/posts",
                    params={"search": title, "status": "publish"},
                    headers=auth, timeout=30)
    r.raise_for_status()
    for p in r.json():
        if title in p.get("title", {}).get("rendered", ""):
            return p["id"]
    return None


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

    existing_id = find_existing_post(session, base, auth, title)
    force = os.environ.get("FORCE_UPDATE") == "1"
    if existing_id:
        if force:
            LOG.info("检测到今日文章(%s)，FORCE_UPDATE 模式：用新内容更新。", existing_id)
            try:
                cat_id = get_category_id(session, base, auth, cat_name)
            except Exception as e:
                LOG.warning("类目解析失败，退回不指定类目: %s", e)
                cat_id = None
            payload = {"title": title, "content": content, "status": "publish"}
            if cat_id:
                payload["categories"] = [cat_id]
            r = session.post(f"{base}/wp-json/wp/v2/posts/{existing_id}",
                             json=payload, headers=auth, timeout=60)
            r.raise_for_status()
            link = r.json().get("link", "")
            LOG.info("已更新(强制): %s", link)
            print("PUBLISHED_URL=" + link)
            return
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
    LOG.info("已发布: %s", link)
    print("PUBLISHED_URL=" + link)


if __name__ == "__main__":
    main()
