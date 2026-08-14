#!/usr/bin/env python3
"""步骤3：把日报 JSON 渲染成文章 HTML，经 WordPress REST API 直接发布（非草稿）。
密钥走环境变量（由 GitHub Secrets 注入），绝不硬编码。

v3 排版规范（卡片式 + 清晰层级）：
- 每个条目为独立卡片，圆角 + 阴影 + 边框，与背景明显区分
- 卡片内层级：
  ・卡片标题 20px/800 字重，最突出
  ・来源/元信息 13px 灰色，带分隔线
  ・字段标签 14px/700 字重，带色点前缀，明显小于正文标题
  ・字段正文 15.5px/400 字重，1.85 行高，阅读舒适
- 模块标题用色块+大字号，含精选计数徽章
- 副业视角用紫色引用块单独突出
- 每日总结用黄色强调卡片
- 内联完整 CSS，高优先级选择器 + !important 防止 WP 主题覆盖
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
/* ── 副业日报 v3 卡片式排版 ── */
.shr-wrap{max-width:760px;margin:0 auto;padding:32px 20px 60px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
  line-height:1.75;color:#111827;background:#f9fafb;box-sizing:border-box;}
.shr-wrap *,.shr-wrap *::before,.shr-wrap *::after{box-sizing:border-box;}

/* 头部 */
.shr-header{text-align:center;padding-bottom:24px;border-bottom:3px solid #e8543f;margin-bottom:28px;}
.shr-kicker{color:#e8543f;font-weight:700;font-size:12px;letter-spacing:2px;text-transform:uppercase;}
.shr-h1{font-size:32px;margin:10px 0 6px;font-weight:900;line-height:1.2;color:#111827;}
.shr-date{color:#6b7280;font-size:14px;}
.shr-lede{background:#fff;border:1px solid #fee2e2;border-left:4px solid #e8543f;border-radius:12px;
  padding:16px 20px;font-size:15px;color:#7f1d1d;margin:24px 0 32px;line-height:1.75;}

/* 模块标题 */
.shr-mod{margin:44px 0 0;}
.shr-mhead{display:flex;align-items:center;gap:10px;margin-bottom:20px;}
.shr-mtag{width:6px;height:28px;border-radius:3px;flex-shrink:0;}
.shr-mtitle{font-size:24px;font-weight:800;margin:0;line-height:1.3;letter-spacing:-.3px;}
.shr-mcount{color:#6b7280;font-size:13px;margin-left:auto;flex-shrink:0;background:#fff;padding:4px 10px;border-radius:20px;border:1px solid #e5e7eb;}
.m1 .shr-mtag{background:#dc2626;}.m1 .shr-mtitle{color:#dc2626;}
.m2 .shr-mtag{background:#2563eb;}.m2 .shr-mtitle{color:#2563eb;}
.m3 .shr-mtag{background:#7c3aed;}.m3 .shr-mtitle{color:#7c3aed;}

/* 模块副标题 */
.shr-msub{color:#6b7280;font-size:14px;margin:-6px 0 18px;line-height:1.6;font-weight:400;}

/* 卡片 */
.shr-card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;
  padding:24px 28px;margin-bottom:20px;
  box-shadow:0 1px 3px rgba(0,0,0,.04),0 6px 16px rgba(0,0,0,.04);
  overflow:hidden;word-wrap:break-word;overflow-wrap:break-word;}
.shr-card:last-child{margin-bottom:0;}

/* 卡片标题 / 来源 */
.shr-it-title{font-size:20px;font-weight:800;color:#111827;margin:0 0 10px!important;line-height:1.4;letter-spacing:-.2px;}
.shr-it-meta{font-size:13px;color:#6b7280;margin:0 0 16px!important;padding-bottom:14px;border-bottom:1px solid #f3f4f6;line-height:1.5;}
.shr-it-meta a{color:#2563eb;text-decoration:none;font-weight:600;}
.shr-it-meta a:hover{text-decoration:underline;}

/* 来源 pill + 阅读按钮 */
.shr-src-pill{display:inline-block;background:#eef2ff;color:#4338ca;font-size:12px;font-weight:700;
  padding:3px 10px;border-radius:999px;margin-right:10px;vertical-align:middle;letter-spacing:.2px;}
.shr-read-btn{display:inline-block;background:#2563eb;color:#fff!important;font-size:13px;font-weight:600;
  padding:4px 12px;border-radius:8px;text-decoration:none!important;vertical-align:middle;}
.shr-read-btn:hover{background:#1d4ed8;text-decoration:none!important;}

/* 字段：标签明显小于正文、颜色区分 */
.shr-field-row{margin-top:18px;}
.shr-field-row:first-of-type{margin-top:0;}
.shr-field-label{font-size:14px;font-weight:700;margin:0 0 6px!important;line-height:1.4;color:#4b5563;}
.shr-field-label::before{content:"▪";margin-right:8px;color:inherit;}
.shr-field-text{font-size:15.5px;color:#374151;margin:0!important;line-height:1.85;}
/* 模块色系 */
.m1 .shr-field-label{color:#dc2626;}
.m2 .shr-field-label{color:#2563eb;}
.m3 .shr-field-label{color:#7c3aed;}

/* 副业视角 */
.shr-persp{background:#f5f3ff;border:1px solid #e9d5ff;border-left:4px solid #7c3aed;border-radius:0 12px 12px 0;padding:16px 20px;margin-top:18px;}
.shr-persp-label{font-size:14px;font-weight:700;color:#7c3aed;margin:0 0 6px!important;}
.shr-persp-text{font-size:15.5px;color:#4c1d95;margin:0!important;line-height:1.85;}

/* 每日总结 */
.shr-summary{background:#fffbeb;border:1px solid #fde68a;border-left:4px solid #d97706;border-radius:16px;padding:22px 26px;margin-top:44px;}
.shr-summary h2{margin:0 0 14px!important;font-size:20px!important;font-weight:800!important;color:#b45309!important;line-height:1.3!important;}
.shr-meth{font-size:15.5px;line-height:1.85!important;color:#374151;margin:0 0 12px!important;}
.shr-ev{margin-top:10px;font-size:13px;color:#6b7280;line-height:1.8!important;}
.shr-ev a{color:#2563eb;text-decoration:none;font-weight:600;margin:0 3px;}
.shr-ev a:hover{text-decoration:underline;}

/* 页脚 */
.shr-footer{margin-top:48px;text-align:center;color:#9ca3af;font-size:12px;padding-top:20px;border-top:1px solid #e5e7eb;}

/* 防止溢出 */
.shr-card img{max-width:100%!important;height:auto!important;border-radius:6px;}
.shr-card table{display:block;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;}
.shr-card pre{white-space:pre-wrap;word-break:break-all;font-size:13px!important;background:#f7f8fa;padding:12px;border-radius:8px;overflow-x:auto;}

/* 响应式 */
@media screen and (max-width:480px){
  .shr-wrap{padding:20px 14px 40px!important;}
  .shr-h1{font-size:26px!important;}
  .shr-mtitle{font-size:20px!important;}
  .shr-card{padding:18px 20px!important;border-radius:14px!important;}
  .shr-it-title{font-size:18px!important;}
  .shr-field-text,.shr-persp-text,.shr-meth{font-size:15px!important;}
  .shr-lede{padding:12px 16px!important;font-size:14px!important;}
  .shr-summary{padding:16px 18px!important;}
}
"""

# ── 模块定义 ──
MODULES = [
    ("project_opportunities", "项目机会库", "m1"),
    ("growth_operations",     "增长运营",   "m2"),
    ("views_insights",        "观点心法",   "m3"),
]

# 模块副标题（显示在模块标题下方，帮助读者快速理解该模块内容）
MODULE_SUBTITLE = {
    "project_opportunities": "能照着做的赚钱项目 / 案例 / 工具",
    "growth_operations": "流量增长、转化与冷启动实操",
    "views_insights": "赚钱存钱的心态、方法与踩坑复盘",
}

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
                f'<div class="persp shr-persp">\n'
                f'<h4 class="persp-label shr-persp-label">副业视角</h4>\n'
                f'<p class="persp-text shr-persp-text">{_esc(val_str)}</p>\n'
                f'</div>\n'
            )
        else:
            rows += (
                f'<div class="field-row shr-field-row">\n'
                f'<h4 class="field-label shr-field-label">{_esc(label)}</h4>\n'
                f'<p class="field-text shr-field-text">{_esc(val_str)}</p>\n'
                f'</div>\n'
            )

    return (
        f'<article class="card shr-card">\n'
        f'<h3 class="it-title shr-it-title">{title}</h3>\n'
        f'<div class="it-meta shr-it-meta">'
        f'<span class="src-pill shr-src-pill">{src_name}</span>'
        f'<a class="read-btn shr-read-btn" href="{src_url}" target="_blank" rel="noopener noreferrer">阅读原文 →</a>'
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
        f'<div class="lede shr-lede">今日精选 <strong>{total}</strong> 条内容，'
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
            f'<p class="msub shr-msub">{_esc(MODULE_SUBTITLE.get(key, ""))}</p>\n'
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


def find_existing_post(session, base, auth, today):
    """返回 (文章id, 内容HTML)；无匹配返回 (None, '')。供防重复/强制更新/自愈判断。

    去重策略（混合触发防重复核心）：
    - 用「今日日期」作为检索词（比整标题稳定；WP 搜索对 '·' 与空格分词不友好，
      整标题检索极易漏匹配，导致"以为没发过又发一篇"）；
    - 再精确比对标题是否同时含「今日日期」与「副业日报」，避免跨日/内容误命中。
    - per_page 放大到 20，覆盖历史同名文章。
    """
    import requests
    try:
        r = session.get(base + "/wp-json/wp/v2/posts",
                        params={"search": today, "status": "publish", "per_page": 20},
                        headers=auth, timeout=30)
        r.raise_for_status()
    except Exception as e:
        LOG.warning("查询已存在文章失败：%s", e)
        return None, ""
    for p in r.json():
        rendered = p.get("title", {}).get("rendered", "")
        if today in rendered and "副业日报" in rendered:
            return p["id"], p.get("content", {}).get("rendered", "")
    return None, ""


def _record_posted(today, post_id, link):
    """回写今日发布状态到 state/last_posted.json（透明化 + 便于排障；失败不影响发布）。"""
    try:
        C.save_json(os.path.join(C.STATE_DIR, "last_posted.json"),
                    {"date": today, "post_id": post_id, "link": link,
                     "run_id": os.environ.get("GITHUB_RUN_ID", "")})
    except Exception as e:
        LOG.warning("回写 last_posted.json 失败（不影响发布）：%s", e)


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

    existing_id, existing_html = find_existing_post(session, base, auth, today)
    force = os.environ.get("FORCE_UPDATE") == "1"
    # 自愈：若已发布文章残缺/为空（无卡片），即便非强制也覆盖修复
    broken = bool(existing_id) and ("shr-card" not in existing_html) and ("<article" not in existing_html)
    if existing_id:
        if force or broken:
            reason = "FORCE_UPDATE" if force else "已发布文章残缺，自愈覆盖"
            LOG.info("检测到今日文章(%s)，%s：用新内容更新。", existing_id, reason)
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
            LOG.info("已更新: %s", link)
            print("PUBLISHED_URL=" + link)
            _record_posted(today, existing_id, link)
            return
        LOG.info("今日文章已存在且完整，跳过发布（防重复）。")
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
    _record_posted(today, r.json().get("id"), link)


if __name__ == "__main__":
    main()
