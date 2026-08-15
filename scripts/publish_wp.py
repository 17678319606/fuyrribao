#!/usr/bin/env python3
"""步骤3：把日报 JSON 渲染成文章 HTML，经 WordPress REST API 直接发布（非草稿）。
密钥走环境变量（由 GitHub Secrets 注入），绝不硬编码。

v4 排版规范（卡片式 + 清晰层级 + 阅读型克制配色）：
- 每个条目为独立卡片，圆角 + 阴影 + 边框，与背景明显区分
- 卡片内层级：
  ・卡片标题 20px/800 字重，最突出
  ・来源/元信息 13px 灰色，带分隔线
  ・字段标签 14px/700 字重，带色点前缀，明显小于正文标题
  ・字段正文 15.5px/400 字重，1.85 行高，阅读舒适
- 模块标题用色块+大字号，含精选计数徽章
- 副业视角用柔和中性底 + 品牌色左边突出
- 每日总结用柔和暖底 + 品牌色左边卡片
- 变现说明用唯一绿色高亮（语义化"钱"）
- 【关键】不使用 <style> 块：WordPress 的 wpautop 会把 <style> 内的 CSS 拆成 <p> 段落，
  导致样式破损与模块头空行。改为每个元素直接挂 inline style（含 !important），
  既压过主题样式、又对 wpautop 完全免疫；响应式靠 clamp() 视口单位实现，无需 @media。
- 内联完整样式，移动端（clamp 流式字号 + clamp 内边距）自动适配，杜绝溢出与排版错乱。
"""
import os
import sys
import json
import time
import html as html_lib
import base64
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

# ── 内联样式常量（不使用 <style> 块，避免 WP wpautop 把 CSS 拆成 <p> 导致样式破损/空行）──
# 全部走 inline style（含 !important），既压过主题样式，又对 wpautop 免疫。
# 响应式靠 clamp()（视口单位）实现，无需 @media 媒体查询。
S_WRAP = ("max-width:760px;width:100%;margin:0 auto!important;"
          "padding:clamp(18px,5vw,36px) clamp(12px,4vw,24px) clamp(36px,8vw,64px)!important;"
          "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei','Noto Sans SC',sans-serif!important;"
          "line-height:1.8!important;color:#2b2b2b!important;background:#faf9f7!important;"
          "box-sizing:border-box!important;-webkit-text-size-adjust:100%!important;")
S_HEADER = "text-align:center;padding-bottom:22px;border-bottom:2px solid #e6e3dd;margin-bottom:28px;"
S_KICKER = "color:#2f6b5e;font-weight:700;font-size:12px;letter-spacing:2px;text-transform:uppercase;"
S_H1 = "font-size:clamp(24px,6.5vw,32px);margin:10px 0 6px;font-weight:900;line-height:1.2;color:#1f1f1f;"
S_DATE = "color:#8a8a8a;font-size:14px;"
S_LEDE = ("background:#ffffff;border:1px solid #e6e3dd;border-left:3px solid #2f6b5e;border-radius:12px;"
          "padding:16px 20px;font-size:15px;color:#4a4a4a;margin:24px 0 32px;line-height:1.78;")
S_MOD = "margin:40px 0 0;"
S_MHEAD = "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:18px;"
S_MTAG = "width:5px;height:26px;border-radius:3px;flex-shrink:0;background:#2f6b5e;"
S_MTITLE = ("font-size:clamp(20px,5vw,24px);font-weight:800;margin:0;line-height:1.3;"
            "color:#2b2b2b;letter-spacing:-.3px;")
S_MCOUNT = ("color:#6b6b6b;font-size:13px;margin-left:auto;flex-shrink:0;background:#ffffff;"
            "padding:4px 10px;border-radius:20px;border:1px solid #e6e3dd;")
S_MSUB = "color:#8a8a8a;font-size:14px;margin:-2px 0 18px;line-height:1.6;font-weight:400;"
S_CARD = ("background:#ffffff;border:1px solid #e6e3dd;border-radius:14px;"
          "padding:clamp(16px,4vw,22px) clamp(14px,4vw,26px);margin-bottom:18px;"
          "box-shadow:0 1px 2px rgba(0,0,0,.03),0 4px 12px rgba(0,0,0,.03);"
          "overflow:hidden;word-wrap:break-word;overflow-wrap:break-word;")
S_IT_TITLE = ("font-size:clamp(18px,4.6vw,20px);font-weight:800;color:#2b2b2b;margin:0 0 10px;"
              "line-height:1.45;letter-spacing:-.2px;")
S_IT_META = ("font-size:13px;color:#8a8a8a;margin:0 0 16px;padding-bottom:14px;"
             "border-bottom:1px solid #f0eee9;line-height:1.5;")
S_SRC_PILL = ("display:inline-block;background:#eef4f1;color:#2f6b5e;font-size:12px;font-weight:700;"
              "padding:3px 10px;border-radius:999px;margin-right:10px;vertical-align:middle;letter-spacing:.2px;")
S_READ_HINT = "font-size:12px;color:#b0aca3;"
S_FIELD_ROW = "margin-top:16px;"
S_FIELD_LABEL = "font-size:14px;font-weight:700;margin:0 0 6px;line-height:1.4;color:#5a5a5a;"
S_FIELD_TEXT = ("font-size:15.5px;color:#3a3a3a;margin:0;line-height:1.85;overflow-wrap:break-word;"
                "word-break:break-word;")
S_BULLET = "color:#2f6b5e;"  # 字段标签前的 ▪ 点
S_MONEY = ("background:#e9f5ef;border:1px solid #9fd4bc;border-left:3px solid #0f7a52;"
           "border-radius:0 12px 12px 0;padding:15px 20px;margin-top:16px;")
S_MONEY_LABEL = "font-size:14px;font-weight:700;margin:0 0 6px;line-height:1.4;color:#0f6b4a;"
S_MONEY_TEXT = ("font-size:15.5px;color:#0f5132;font-weight:500;margin:0;line-height:1.85;"
                "overflow-wrap:break-word;word-break:break-word;")
S_PERSP = ("background:#f4f2ed;border:1px solid #e3ded3;border-left:3px solid #2f6b5e;"
           "border-radius:0 12px 12px 0;padding:16px 20px;margin-top:16px;")
S_PERSP_LABEL = "font-size:14px;font-weight:700;color:#2f6b5e;margin:0 0 6px;line-height:1.4;"
S_PERSP_TEXT = ("font-size:15.5px;color:#33433e;margin:0;line-height:1.85;overflow-wrap:break-word;"
                "word-break:break-word;")
S_SUMMARY = ("background:#f4f2ed;border:1px solid #e3ded3;border-left:3px solid #2f6b5e;"
             "border-radius:14px;padding:clamp(18px,4vw,22px) clamp(18px,4vw,26px);margin-top:40px;")
S_SUMMARY_H2 = "font-size:clamp(18px,4.6vw,20px);font-weight:800;color:#2b2b2b;margin:0 0 14px;line-height:1.3;"
S_METH = ("font-size:15.5px;line-height:1.85;color:#3a3a3a;margin:0 0 12px;overflow-wrap:break-word;"
          "word-break:break-word;")
S_EV = "margin-top:10px;font-size:13px;color:#8a8a8a;line-height:1.8;"
S_TEXT_LINK = "color:#2f6b5e;font-weight:600;"
S_FOOTER = "margin-top:48px;text-align:center;color:#b0aca3;font-size:12px;padding-top:20px;border-top:1px solid #e6e3dd;"

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
    """渲染单条卡片（inline 样式，空字段不展示）。"""
    if not isinstance(it, dict):
        # 极端兜底：非字典条目（AI 偶发返回字符串）直接转纯文本卡片，避免崩溃
        it = {"title": str(it)[:200]}
    title = _esc(it.get("title", ""))
    src_name = _esc(it.get("source_name", ""))
    src_url = it.get("source_url", "")

    rows = ""
    for key, label in CANVAS_FIELDS:
        val = it.get(key)
        if not val or not str(val).strip():
            continue  # 空字段：跳过，不输出标题也不占位

        val_str = str(val).strip()
        bullet = f'<span style="{S_BULLET}">▪</span> '
        if key == "perspective":
            rows += (
                f'<div style="{S_PERSP}">\n'
                f'<h4 style="{S_PERSP_LABEL}">副业视角</h4>\n'
                f'<p style="{S_PERSP_TEXT}">{_esc(val_str)}</p>\n'
                f'</div>\n'
            )
        elif key == "monetization":
            rows += (
                f'<div style="{S_MONEY}">\n'
                f'<h4 style="{S_MONEY_LABEL}">{bullet}{_esc(label)}</h4>\n'
                f'<p style="{S_MONEY_TEXT}">{_esc(val_str)}</p>\n'
                f'</div>\n'
            )
        else:
            rows += (
                f'<div style="{S_FIELD_ROW}">\n'
                f'<h4 style="{S_FIELD_LABEL}">{bullet}{_esc(label)}</h4>\n'
                f'<p style="{S_FIELD_TEXT}">{_esc(val_str)}</p>\n'
                f'</div>\n'
            )

    # 来源链接：有 source_url 则渲染为可点击链接；无则降级为纯文本提示
    if src_url:
        src_html = (
            f'<span style="{S_SRC_PILL}">{src_name}</span> '
            f'<a href="{_esc(src_url)}" target="_blank" rel="noopener" data-fuyr-src="1" '
            f'style="font-size:12px;color:#2f6b5e;font-weight:600;text-decoration:none;">'
            f'阅读原文 ↗</a>'
        )
    else:
        src_html = (
            f'<span style="{S_SRC_PILL}">{src_name}</span>'
            f'<span style="{S_READ_HINT}">来源文字链 · 阅读原文请返回对应平台</span>'
        )

    return (
        f'<article style="{S_CARD}">\n'
        f'<h3 style="{S_IT_TITLE}">{title}</h3>\n'
        f'<div style="{S_IT_META}">'
        f'{src_html}'
        f'</div>\n'
        f'{rows}'
        f'</article>'
    )


def _strip_links(html):
    """后处理：把 AI 生成的无关 <a> 标签转为纯文本（保留模板注入的来源链接）。

    保留规则：带 data-fuyr-src 属性的 <a> 为模板注入的「阅读原文」链接，不删除。
    其余所有 <a>（AI 输出中可能夹带的推广/参考链接）全部脱链为文本。
    """
    import re
    # 1) 保留带 data-fuyr-src 的来源链接（模板注入），其余 <a> 脱链
    html = re.sub(
        r'<a\b(?![^>]*data-fuyr-src)[^>]*?href="[^"]*"[^>]*?>(.*?)</a>',
        r'<span style="%s">\1</span>' % S_TEXT_LINK, html, flags=re.S | re.I)
    html = re.sub(
        r'<a\b(?![^>]*data-fuyr-src)[^>]*?>(.*?)</a>',
        r'<span style="%s">\1</span>' % S_TEXT_LINK,
        html, flags=re.S | re.I)
    # 2) 把正文里"可见的裸 URL"替换为空（保持阅读清爽、无外链）。
    #    关键修复：
    #    a) 负向后顾 (?<!\bhref=")(?<!\bsrc=") 排除 <a href="..."> / <img src="..."> 里的 URL，
    #       否则会把"阅读原文"等来源链接的 href 一并清空（导致链接点不动）——此前已上线版本存在此回归；
    #    b) 字符集排除 CJK（一-鿿），URL 遇到中文即停，避免误吞紧跟其后的中文正文
    #       （这正是"长内容末尾被轻微截断"的根因之一）。
    html = re.sub(r'(?<!href=")(?<!src=")https?://[^\s<>"\')一-鿿]+', '', html, flags=re.I)
    # 3) 清理可能留下的空括号、多余空白
    html = re.sub(r'\(\s*\)', '', html)
    html = re.sub(r'[ ]{2,}', ' ', html)
    return html


def render(report):
    """将完整 report JSON 渲染为带内联样式的 HTML 片段（正文不含可点击链接与裸 URL）。"""
    date = report.get("date", C.date_str())
    total = sum(len(report.get("modules", {}).get(k, [])) for k, _, _ in MODULES)

    lede = (
        f'<div style="{S_LEDE}">今日精选 <strong>{total}</strong> 条内容，'
        f'按「项目机会库 / 增长运营 / 观点心法」分模块呈现。</div>\n'
    )

    body = (
        f'<header style="{S_HEADER}">\n'
        f'<div style="{S_KICKER}">AI 副业日报</div>\n'
        f'<h1 style="{S_H1}">副业日报 · {_esc(date)}</h1>\n'
        f'<div style="{S_DATE}">{_esc(date)}（北京时间）· 自动生成</div>\n'
        f'</header>\n'
        f'{lede}'
    )

    for key, mtitle, cls in MODULES:
        items = report.get("modules", {}).get(key, [])
        if not items:
            continue
        cards = "".join(render_item(it, cls) for it in items)
        body += (
            f'<section style="{S_MOD}">\n'
            f'<div style="{S_MHEAD}">'
            f'<span style="{S_MTAG}"></span>'
            f'<h2 style="{S_MTITLE}">{mtitle}</h2>'
            f'<span style="{S_MCOUNT}">精选 {len(items)}</span>'
            f'</div>\n'
            f'<p style="{S_MSUB}">{_esc(MODULE_SUBTITLE.get(key, ""))}</p>\n'
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
            for it in report.get("modules", {}).get(k, []):
                u = it.get("source_url")
                if u:
                    url_map[u] = (it.get("source_name", ""), it.get("title", ""))
        ev_parts = []
        for u in ds.get("evidence", []):
            sn, ti = url_map.get(u, ("", ""))
            label = ti or sn or _domain(u)
            ev_parts.append(f'<span style="{S_TEXT_LINK}">{_esc(label)}</span>')
        ev_html = " · ".join(ev_parts)

        body += (
            f'<section><div style="{S_SUMMARY}">\n'
            f'<h2 style="{S_SUMMARY_H2}">每日总结 · 可复用方法论</h2>\n'
            f'<div style="{S_METH}">{_esc(meth)}</div>\n'
            f'<div style="{S_EV}">证据：{ev_html}</div>\n'
            f'</div></section>\n'
        )

    html = (
        f'<!-- dr-renderer:{C.RENDERER_VERSION} -->'
        f'<div style="{S_WRAP}">\n'
        f'{body}\n'
        f'<footer style="{S_FOOTER}">本文由 GitHub Actions 自动生成并发布。</footer>\n'
        f'</div>'
    )
    cleaned = _strip_links(html)
    # 去除标签间空白，进一步减少 WP wpautop 插入多余 <p> 的几率（不删除正文文字）
    import re as _re
    cleaned = _re.sub(r'>\s+<', '><', cleaned)
    return cleaned


def _short(url, n=64):
    return url if len(url) <= n else url[:n] + "…"


def _wp_call(session, method, url, auth, json_body=None, timeout=90, retries=2, backoff=5):
    """WP REST 调用包装：偶发超时/连接抖动/429/5xx 自动重试，退避线性增长。
    慢文章发布（大 HTML）给 90s 超时；绝不因瞬时抖动导致当天发布失败。"""
    import requests
    last_err = None
    for i in range(1, retries + 1):
        try:
            kw = {"headers": auth, "timeout": timeout}
            if json_body is not None:
                kw["json"] = json_body
            r = session.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = backoff * i
                LOG.warning("WP %s %s 返回 %s，%ds 后重试", method, _short(url), r.status_code, wait)
                time.sleep(wait)
                last_err = RuntimeError("wp status %s" % r.status_code)
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            wait = backoff * i
            LOG.warning("WP %s 连接失败(%s)，%ds 后重试", method, type(e).__name__, wait)
            if i < retries:
                time.sleep(wait)
    raise last_err or RuntimeError("wp call failed: " + url)


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
    """返回 (文章id, 内容HTML)；无匹配返回 (None, '')。供覆盖更新/自愈判断。

    检索策略：
    - 同时查 publish 与 trash 状态——这样即便文章被误移回收站，
      重跑也能定位到同一篇并「恢复+覆盖更新」，文章链接(slug)保持不变；
    - 用「今日日期」作为检索词（比整标题稳定；WP 搜索对 '·' 与空格分词不友好，
      整标题检索极易漏匹配，导致"以为没发过又发一篇"）；
    - 再精确比对标题是否含「今日日期」与「副业日报」，避免跨日/内容误命中。
    """
    import requests
    found = []
    for st in ("publish", "trash"):
        try:
            r = session.get(base + "/wp-json/wp/v2/posts",
                            params={"search": today, "status": st, "per_page": 20},
                            headers=auth, timeout=30)
            r.raise_for_status()
            found.extend(r.json())
        except Exception as e:
            LOG.warning("查询已存在文章(%s)失败：%s", st, e)
    for p in found:
        rendered = p.get("title", {}).get("rendered", "")
        pdate = (p.get("date") or "")[:10]
        # 双重判定：标题含「今日+副业日报」或「发布日期=今日+副业日报」。
        # 用 post_date 兜底，避免标题日期格式变化导致漏匹配而重复发文。
        if "副业日报" in rendered and (today in rendered or pdate == today):
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
    # 发布开关：FUYR_DISABLE_PUBLISH=1 时仅渲染不发布（用于 CNB 等副流水线，避免双 CI 同发一个 WP 造成重复/覆盖）。
    # 让 GitHub Actions 成为唯一发布方；CNB 仅做"生成+诊断"，互不打架。
    if os.environ.get("FUYR_DISABLE_PUBLISH", "").strip() in ("1", "true", "True", "yes"):
        LOG.info("FUYR_DISABLE_PUBLISH=1：跳过 WordPress 发布（仅校验/渲染），不触碰线上文章。")
        return
    today = C.date_str()
    # 同日增量累积：若 generate 判定无变化（无新增信号且非强制重渲染），跳过重渲染 WP 以免冗余更新。
    flag_path = os.path.join(C.STATE_DIR, ".gen_changed")
    try:
        gen_changed = open(flag_path, encoding="utf-8").read().strip() == "1"
    except Exception:
        gen_changed = True  # 本地手动运行无标记时默认发布（兼容旧行为）
    if not gen_changed:
        LOG.info("generate 标记无变化（.gen_changed=0），跳过 WordPress 发布。")
        return
    report = C.load_json(os.path.join(C.DATA_DIR, f"report-{today}.json"), {})
    if not report:
        LOG.error("无日报数据，跳过发布。")
        raise SystemExit("no report data")
    if report.get("ai_failed"):
        LOG.error("report 为降级/AI 失败状态，按策略不发布非 AI 内容。")
        raise SystemExit("ai failed report rejected")
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
    # 同日多次执行 → 覆盖更新同一篇文章（保持 slug/URL 不变）；无同日文章则新建。
    # 永不产生重复文章（始终按 existing_id 更新，而非新建第二篇）。
    if existing_id:
        LOG.info("检测到今日文章(%s)，用新内容覆盖更新（文章链接保持不变）。", existing_id)
        try:
            cat_id = get_category_id(session, base, auth, cat_name)
        except Exception as e:
            LOG.warning("类目解析失败，退回不指定类目: %s", e)
            cat_id = None
        payload = {"title": title, "content": content, "status": "publish"}
        if cat_id:
            payload["categories"] = [cat_id]
        r = _wp_call(session, "POST", f"{base}/wp-json/wp/v2/posts/{existing_id}",
                     auth, json_body=payload, timeout=90)
        r.raise_for_status()
        link = r.json().get("link", "")
        LOG.info("已更新: %s", link)
        print("PUBLISHED_URL=" + link)
        _record_posted(today, existing_id, link)
        return

    try:
        cat_id = get_category_id(session, base, auth, cat_name)
    except Exception as e:
        LOG.warning("类目解析失败，退回不指定类目: %s", e)
        cat_id = None

    payload = {"title": title, "content": content, "status": "publish"}
    if cat_id:
        payload["categories"] = [cat_id]
    r = _wp_call(session, "POST", base + "/wp-json/wp/v2/posts",
                 auth, json_body=payload, timeout=90)
    r.raise_for_status()
    link = r.json().get("link", "")
    LOG.info("已发布: %s", link)
    print("PUBLISHED_URL=" + link)
    _record_posted(today, r.json().get("id"), link)


if __name__ == "__main__":
    main()
