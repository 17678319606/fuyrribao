#!/usr/bin/env python3
"""单条内容二次发布（republish）：把日报里"合格"的单条，单独发成 WordPress 薄文。

目的（提分点：内容资产化 / 长尾 SEO / 单条可分享）：
  - 日报是"聚合卡片"，单条好内容淹没其中；单独发薄文可被搜索引擎收录、可被读者分享。
  - 防 SEO 内耗：薄文注入 <meta robots=noindex,follow> + canonical=source_url（不抢日报主帖、
    也不与源站竞争排名）。
  - 防重复：按 source_url 去重（state/republished.json），同一源 URL 永不发第二篇薄文；
    且 WP 端发布走同源鉴权，重复运行安全。
  - 质量门：仅发布"非空心"且含 source_url 的合格单条（复用 generate_report._is_hollow_item
    实战验证逻辑，避免把垃圾/空心也发成薄文）。
  - 默认关闭：必须 FUYR_REPUBLISH=1 才启用；不设置则本脚本直接 return（零侵入）。
  - 限额：每天最多发 MAX_REPUBLISH_PER_DAY 条（默认 10），灰度安全。
  - 全程非阻塞：run_daily 以 try/except 包裹，任何异常不影响主流程。

依赖：复用 publish_wp 的 WP REST 封装（_wp_call / get_category_id）与品牌样式常量。
"""
import os
import sys
import json
import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import publish_wp as PW
import generate_report as GR
import score as SC

LOG = C.get_logger()

MAX_REPUBLISH_PER_DAY = 10     # 每日灰度上限
REPUBLISHED_FILE = os.path.join(C.STATE_DIR, "republished.json")

# 模块 → WP 类目中文名（与 publish_wp 类目体系对齐；薄文归入对应模块类目，便于归档）
MODULE_CAT = {
    "project_opportunities": "项目机会库",
    "growth_operations": "增长运营",
    "views_insights": "观点心法",
}


def _esc(s):
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _load_republished():
    return set(C.load_json(REPUBLISHED_FILE, []) or [])


def _save_republished(urls):
    C.save_json(REPUBLISHED_FILE, sorted(urls))


def select_qualified(report):
    """返回 [(module_key, item)] 合格单条列表（非空心 + 含 source_url）。

    合格判定复用 generate_report._is_hollow_item（已实战验证的空心检测），
    而不是 score.generic_score 的 composite（其量纲为 0~23 且 recency 恒为常量，
    直接作硬门槛会误选空心条目）。
    """
    out = []
    # 注意：common.MODULES 是模块 key 字符串元组（"project_opportunities" 等），
    # 与 publish_wp.MODULES（(key,title,cls) 元组）不同，这里直接遍历 key。
    for mk in C.MODULES:
        for it in (report.get("modules", {}).get(mk) or []):
            if not isinstance(it, dict):
                continue
            url = (it.get("source_url") or "").strip()
            if not url.startswith("http"):
                continue
            hollow, _reason = GR._is_hollow_item(it)
            if hollow:
                continue
            out.append((mk, it))
    return out


def render_single(item, module_key):
    """渲染单条薄文 HTML（复用日报品牌样式常量，inline style 抗 wpautop）。"""
    title = _esc(item.get("title", ""))
    src = _esc(item.get("source_name", ""))
    url = (item.get("source_url") or "").strip()
    # canonical + noindex（防 SEO 内耗，不抢日报主帖排名）
    meta = ""
    if url:
        meta = ('<link rel="canonical" href="%s" />\n'
                '<meta name="robots" content="noindex,follow" />\n' % _esc(url))

    fields = [
        ("signal", "信号", item.get("signal")),
        ("target_customer", "目标客户", item.get("target_customer")),
        ("value_proposition", "价值主张", item.get("value_proposition")),
        ("how_to_mvp", "怎么做 MVP", item.get("how_to_mvp")),
        ("acquisition_channel", "获客渠道", item.get("acquisition_channel")),
        ("startup_cost", "启动成本", item.get("startup_cost")),
        ("replicability", "可复制性", item.get("replicability")),
    ]
    body = ""
    for _key, label, val in fields:
        val = (val or "").strip()
        if not val:
            continue
        body += (
            f'<div style="{PW.S_FIELD_ROW}">'
            f'<div style="{PW.S_FIELD_LABEL}"><span style="{PW.S_BULLET}">▪ </span>{label}</div>'
            f'<p style="{PW.S_FIELD_TEXT}">{_esc(val)}</p></div>\n'
        )
    mon = (item.get("monetization") or "").strip()
    if mon:
        body += (
            f'<div style="{PW.S_MONEY}">'
            f'<div style="{PW.S_MONEY_LABEL}">变现说明</div>'
            f'<p style="{PW.S_MONEY_TEXT}">{_esc(mon)}</p></div>\n'
        )
    per = (item.get("perspective") or "").strip()
    if per:
        body += (
            f'<div style="{PW.S_PERSP}">'
            f'<div style="{PW.S_PERSP_LABEL}">观点心法</div>'
            f'<p style="{PW.S_PERSP_TEXT}">{_esc(per)}</p></div>\n'
        )
    cat_cn = MODULE_CAT.get(module_key, "")
    meta_pill = f'<span style="{PW.S_SRC_PILL}">{_esc(src)}</span>' if src else ""
    cat_pill = f'<span style="{PW.S_SRC_PILL}">{_esc(cat_cn)}</span>' if cat_cn else ""
    src_link = (f'<a href="{_esc(url)}" style="{PW.S_TEXT_LINK}">{_esc(src or url)}</a>'
                if url else _esc(src))
    html = (
        f'<!-- fuyr-single -->\n'
        f'{meta}'
        f'<div style="{PW.S_WRAP}">\n'
        f'<div style="{PW.S_LEDE}">本文由「副业日报」自动提炼的{_esc(cat_cn)}单条干货，'
        f'仅供学习参考；原文见底部来源。</div>\n'
        f'<article style="{PW.S_CARD}">\n'
        f'<h1 style="{PW.S_IT_TITLE}">{title}</h1>\n'
        f'<div style="{PW.S_IT_META}">{meta_pill}{cat_pill}'
        f'<span style="{PW.S_READ_HINT}"> · 副业日报单条提炼</span></div>\n'
        f'{body}\n'
        f'<div style="{PW.S_IT_META}">来源：{src_link}</div>\n'
        f'</article>\n'
        f'<footer style="{PW.S_FOOTER}">本篇为「副业日报」单条内容提炼，noindex 不参与搜索排名。</footer>\n'
        f'</div>'
    )
    return html


def _derive_tags(item, module_key):
    tags = [MODULE_CAT.get(module_key, "")]
    sn = (item.get("source_name") or "").strip()
    if sn:
        tags.append(sn)
    return [t for t in tags if t]


def publish_one(session, base, auth, item, module_key):
    """发布单条薄文到 WP，返回文章链接。网络调用集中在 PW._wp_call / session。"""
    html = render_single(item, module_key)
    title = (item.get("title") or "副业日报单条")[:80]
    cat_name = MODULE_CAT.get(module_key, "")
    try:
        cat_id = PW.get_category_id(session, base, auth, cat_name) if cat_name else None
    except Exception as e:
        LOG.warning("类目解析失败，退回不指定类目: %s", e)
        cat_id = None
    payload = {
        "title": title,
        "content": html,
        "status": "publish",
        # 多插件兼容的 noindex 标记（无插件时内容内 <meta robots> 仍生效）
        "meta_input": {
            "robots": "noindex,follow",
            "_yoast_wpseo_meta-robots-noindex": "1",
            "_rankmath_robots": "noindex",
        },
    }
    if cat_id:
        payload["categories"] = [cat_id]
    tags = _derive_tags(item, module_key)
    if tags:
        try:
            tids = []
            for t in tags:
                r = session.get(base + "/wp-json/wp/v2/tags",
                                 params={"search": t}, headers=auth, timeout=30)
                r.raise_for_status()
                data = r.json()
                if data:
                    tids.append(data[0]["id"])
                else:
                    r2 = session.post(base + "/wp-json/wp/v2/tags",
                                      json={"name": t}, headers=auth, timeout=30)
                    r2.raise_for_status()
                    tids.append(r2.json()["id"])
            if tids:
                payload["tags"] = tids
        except Exception as e:
            LOG.warning("标签处理失败（不影响发布）: %s", e)
    r = PW._wp_call(session, "POST", base + "/wp-json/wp/v2/posts",
                    auth, json_body=payload, timeout=90)
    r.raise_for_status()
    return r.json().get("link", "")


def main():
    # 默认关闭：不设置 FUYR_REPUBLISH 则零侵入退出
    if os.environ.get("FUYR_REPUBLISH", "").strip() not in ("1", "true", "True", "yes"):
        LOG.info("FUYR_REPUBLISH 未开启（默认关闭），跳过单条二次发布。")
        return
    C.ensure_dirs()
    today = C.date_str()
    report = C.load_json(os.path.join(C.DATA_DIR, f"report-{today}.json"), {})
    if not report or not isinstance(report, dict):
        LOG.info("无日报数据，跳过单条发布。")
        return

    qualified = select_qualified(report)
    if not qualified:
        LOG.info("今日无合格单条（非空心且含 source_url），跳过单条发布。")
        return

    # 去重：已发过的 source_url 不再发
    republished = _load_republished()
    todo = [(mk, it) for mk, it in qualified
            if (it.get("source_url") or "").strip() not in republished]
    if not todo:
        LOG.info("合格单条均已发过（去重命中），跳过。")
        return
    todo = todo[:MAX_REPUBLISH_PER_DAY]
    LOG.info("待发布合格单条 %d 条（上限 %d）", len(todo), MAX_REPUBLISH_PER_DAY)

    wp_url = os.environ.get("WP_URL", "https://dajiayouxuan.com").rstrip("/")
    user = os.environ.get("WP_USER", "tougao")
    app_pw = os.environ.get("WP_APP_PASSWORD", "")
    if not app_pw:
        LOG.warning("缺少 WP_APP_PASSWORD，无法发布单条薄文。")
        return
    import requests
    session = requests.Session()
    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{app_pw}".encode()).decode(), "Content-Type": "application/json"}

    done = []
    for mk, it in todo:
        try:
            link = publish_one(session, wp_url, auth, it, mk)
            republished.add((it.get("source_url") or "").strip())
            done.append(it.get("title", "")[:30])
            LOG.info("✓ 单条已发布: %s -> %s", (it.get("title") or "")[:40], link)
        except Exception as e:
            LOG.warning("单条发布失败（跳过，不影响其余）: %s | %s",
                        (it.get("title") or "")[:40], e)
    if done:
        _save_republished(republished)
        LOG.info("本日单条二次发布完成 %d 条", len(done))
    print("REPUBLISH_COUNT=%d" % len(done))


if __name__ == "__main__":
    main()
