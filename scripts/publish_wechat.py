#!/usr/bin/env python3
"""步骤4（公众号推送）：把日报 JSON 渲染为图文 HTML，上传封面素材 + 图文，按账号配置推送。

多账号支持（环境变量，二选一）：
  - WX_ACCOUNTS（推荐，多账号）：JSON 数组，每个账号：
      [{"name":"个人订阅号","appid":"wx...","secret":"...","mode":"freepublish","default":true},
       {"name":"认证订阅号","appid":"wx...","secret":"...","mode":"mass"}]
    mode 决定该账号的推送方式；default:true 的账号为「主账号」（WX_PUSH_ALL=true 时全部推送）。
  - 单账号兼容（旧式）：WX_APPID + WX_APPSECRET + WX_PUBLISH_MODE。

三模式（每账号各自 mode，或单账号 WX_PUBLISH_MODE 切换）：
  - mass        : 认证订阅号/认证服务号 API 群发（message/mass/sendall，粉丝直接收到未读）
  - freepublish : 发布到公众号（freepublish/submit，不占群发次数；粉丝需点开看）【默认】
  - draft       : 仅上传到草稿箱（draft/add），你在后台点一下「群发」即可（半自动）

「认证号总类」预留说明（微信平台规则）：
  - 认证订阅号：每天可 mass 群发 1 次 → 用 mode=mass，粉丝直接收到未读（最贴合每日自动群发）。
  - 认证服务号：每月仅群发 4 次 → 同样可用 mode=mass，但额度不足日更，建议仅精选 4 篇/月或改用 freepublish。
  - 个人/未认证订阅号：无 mass 权限（48001）→ 只能用 freepublish / draft。
  代码对三类均兼容，差异仅在账号类型对应的 mode 与微信后台额度。

与 WP 发布的关系（单仓双推、AI 只跑一次）：
  - fetch_signals → generate_report（AI 增量）→ publish_wp（增量更新同日文章）→ publish_wechat（本脚本）
  - 本脚本门控，确保不浪费、不重复：
    1) 未配置任何账号（WX_ACCOUNTS / WX_APPID）→ 仅发 WP，跳过公众号；
    2) 当前北京小时 < WX_FINAL_HOUR（默认 19）→ 跳过，等当日末次触发再发最全版；
    3) 当日已推送过的账号（state/wechat_published_{date}.json 记录）→ 跳过，绝不一天多篇。

安全：appid/secret 走仓库「环境变量 / Secrets」，绝不硬编码。
封面：由 generate_cover.make_cover 动态生成「副业日报 + 日期」，上传为 thumb 素材。
"""
import os
import sys
import json
import time
import html as html_lib
import urllib.parse
import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from generate_cover import make_cover

LOG = C.get_logger()

WX_API = "https://api.weixin.qq.com/cgi-bin"

# 微信 IP 白名单代理：GitHub Actions 动态 IP 不在白名单时，
# 通过固定出口服务器（如反代/源站）代理微信 API 请求。
# 设置 WX_PROXY_URL（含协议，如 http://x.x.x.x:8888）即可启用；留空则直连。
_WX_PROXY = os.environ.get("WX_PROXY_URL", "").strip()
_WX_PROXIES = {"http": _WX_PROXY, "https": _WX_PROXY} if _WX_PROXY else None

if _WX_PROXY:
    LOG.info("微信 API 代理已启用: %s（出口 IP 将匹配白名单）", _WX_PROXY)
else:
    LOG.info("微信 API 直连模式（无代理）")

MODULES = [
    ("project_opportunities", "项目机会库"),
    ("growth_operations", "增长运营"),
    ("views_insights", "观点心法"),
]
MODULE_SUB = {
    "project_opportunities": "能照着做的赚钱项目 / 案例 / 工具",
    "growth_operations": "流量增长、转化与冷启动实操",
    "views_insights": "赚钱存钱的心态、方法与踩坑复盘",
}
CANVAS_FIELDS = [
    ("signal", "信号 / 趋势"),
    ("target_customer", "目标客户"),
    ("value_proposition", "价值主张"),
    ("how_to_mvp", "建议怎么做 / MVP"),
    ("acquisition_channel", "获客渠道"),
    ("monetization", "变现说明与数据表现"),
    ("startup_cost", "启动成本"),
    ("replicability", "可复制性 / 壁垒"),
    ("perspective", "副业视角"),
]

# WeChat 正文固定宽度 677px，全部 inline 样式（微信会剥离 <style> 块，且 clamp 支持不稳，统一用 px）
S_WRAP = ("max-width:677px;width:100%;margin:0 auto!important;"
          "padding:24px 16px 48px!important;"
          "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei','Noto Sans SC',sans-serif!important;"
          "line-height:1.8!important;color:#2b2b2b!important;background:#faf9f7!important;"
          "box-sizing:border-box!important;-webkit-text-size-adjust:100%!important;")
S_HEADER = "text-align:center;padding-bottom:22px;border-bottom:2px solid #e6e3dd;margin-bottom:28px;"
S_KICKER = "color:#2f6b5e;font-weight:700;font-size:12px;letter-spacing:2px;"
S_H1 = "font-size:28px;margin:10px 0 6px;font-weight:900;line-height:1.2;color:#1f1f1f;"
S_DATE = "color:#8a8a8a;font-size:14px;"
S_LEDE = ("background:#ffffff;border:1px solid #e6e3dd;border-left:3px solid #2f6b5e;border-radius:12px;"
          "padding:16px 20px;font-size:15px;color:#4a4a4a;margin:24px 0 32px;line-height:1.78;")
S_MOD = "margin:40px 0 0;"
S_MHEAD = "display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:18px;"
S_MTAG = "width:5px;height:26px;border-radius:3px;flex-shrink:0;background:#2f6b5e;"
S_MTITLE = "font-size:22px;font-weight:800;margin:0;line-height:1.3;color:#2b2b2b;"
S_MCOUNT = ("color:#6b6b6b;font-size:13px;margin-left:auto;flex-shrink:0;background:#ffffff;"
            "padding:4px 10px;border-radius:20px;border:1px solid #e6e3dd;")
S_MSUB = "color:#8a8a8a;font-size:14px;margin:-2px 0 18px;line-height:1.6;"
S_CARD = ("background:#ffffff;border:1px solid #e6e3dd;border-radius:14px;"
          "padding:20px 22px;margin-bottom:18px;box-shadow:0 1px 2px rgba(0,0,0,.03),0 4px 12px rgba(0,0,0,.03);"
          "overflow:hidden;word-wrap:break-word;overflow-wrap:break-word;")
S_IT_TITLE = "font-size:20px;font-weight:800;color:#2b2b2b;margin:0 0 10px;line-height:1.45;"
S_IT_META = "font-size:13px;color:#8a8a8a;margin:0 0 16px;padding-bottom:14px;border-bottom:1px solid #f0eee9;line-height:1.5;"
S_SRC_PILL = ("display:inline-block;background:#eef4f1;color:#2f6b5e;font-size:12px;font-weight:700;"
              "padding:3px 10px;border-radius:999px;margin-right:10px;vertical-align:middle;")
S_READ_HINT = "font-size:12px;color:#b0aca3;"
S_FIELD_ROW = "margin-top:16px;"
S_FIELD_LABEL = "font-size:14px;font-weight:700;margin:0 0 6px;line-height:1.4;color:#5a5a5a;"
S_FIELD_TEXT = "font-size:15.5px;color:#3a3a3a;margin:0;line-height:1.85;overflow-wrap:break-word;word-break:break-word;"
S_BULLET = "color:#2f6b5e;"
S_MONEY = ("background:#e9f5ef;border:1px solid #9fd4bc;border-left:3px solid #0f7a52;border-radius:0 12px 12px 0;padding:15px 20px;margin-top:16px;")
S_MONEY_LABEL = "font-size:14px;font-weight:700;margin:0 0 6px;line-height:1.4;color:#0f6b4a;"
S_MONEY_TEXT = "font-size:15.5px;color:#0f5132;font-weight:500;margin:0;line-height:1.85;overflow-wrap:break-word;word-break:break-word;"
S_PERSP = ("background:#f4f2ed;border:1px solid #e3ded3;border-left:3px solid #2f6b5e;border-radius:0 12px 12px 0;padding:16px 20px;margin-top:16px;")
S_PERSP_LABEL = "font-size:14px;font-weight:700;color:#2f6b5e;margin:0 0 6px;line-height:1.4;"
S_PERSP_TEXT = "font-size:15.5px;color:#33433e;margin:0;line-height:1.85;overflow-wrap:break-word;word-break:break-word;"
S_SUMMARY = ("background:#f4f2ed;border:1px solid #e3ded3;border-left:3px solid #2f6b5e;border-radius:14px;padding:20px 22px;margin-top:40px;")
S_SUMMARY_H2 = "font-size:20px;font-weight:800;color:#2b2b2b;margin:0 0 14px;line-height:1.3;"
S_METH = "font-size:15.5px;line-height:1.85;color:#3a3a3a;margin:0 0 12px;overflow-wrap:break-word;word-break:break-word;"
S_EV = "margin-top:10px;font-size:13px;color:#8a8a8a;line-height:1.8;"
S_TEXT_LINK = "color:#2f6b5e;font-weight:600;"
S_FOOTER = "margin-top:48px;text-align:center;color:#b0aca3;font-size:12px;padding-top:20px;border-top:1px solid #e6e3dd;"


def _esc(s):
    return html_lib.escape(str(s)) if s else ""


def _domain(u):
    try:
        return urllib.parse.urlparse(u).netloc.replace("www.", "")
    except Exception:
        return u


def _strip_links(html):
    import re
    html = re.sub(r'<a\b[^>]*?href="[^"]*"[^>]*?>(.*?)</a>',
                  r'<span style="%s">\1</span>' % S_TEXT_LINK, html, flags=re.S | re.I)
    html = re.sub(r'<a\b[^>]*?>(.*?)</a>', r'<span style="%s">\1</span>' % S_TEXT_LINK,
                  html, flags=re.S | re.I)
    html = re.sub(r'https?://[^\s<>"\')]+', '', html, flags=re.I)
    html = re.sub(r'\(\s*\)', '', html)
    html = re.sub(r'[ ]{2,}', ' ', html)
    return html


def render_item(it):
    if not isinstance(it, dict):
        it = {"title": str(it)[:200]}
    title = _esc(it.get("title", ""))
    src_name = _esc(it.get("source_name", ""))
    rows = ""
    for key, label in CANVAS_FIELDS:
        val = it.get(key)
        if not val or not str(val).strip():
            continue
        val_str = str(val).strip()
        bullet = f'<span style="{S_BULLET}">▪</span> '
        if key == "perspective":
            rows += (f'<div style="{S_PERSP}"><h4 style="{S_PERSP_LABEL}">副业视角</h4>'
                     f'<p style="{S_PERSP_TEXT}">{_esc(val_str)}</p></div>')
        elif key == "monetization":
            rows += (f'<div style="{S_MONEY}"><h4 style="{S_MONEY_LABEL}">{bullet}{_esc(label)}</h4>'
                     f'<p style="{S_MONEY_TEXT}">{_esc(val_str)}</p></div>')
        else:
            rows += (f'<div style="{S_FIELD_ROW}"><h4 style="{S_FIELD_LABEL}">{bullet}{_esc(label)}</h4>'
                     f'<p style="{S_FIELD_TEXT}">{_esc(val_str)}</p></div>')
    return (f'<article style="{S_CARD}"><h3 style="{S_IT_TITLE}">{title}</h3>'
            f'<div style="{S_IT_META}"><span style="{S_SRC_PILL}">{src_name}</span>'
            f'<span style="{S_READ_HINT}">来源文字链 · 阅读原文请返回对应平台</span></div>'
            f'{rows}</article>')


def render_wechat(report):
    date = report.get("date", C.date_str())
    total = sum(len(report.get("modules", {}).get(k, [])) for k, _ in MODULES)
    lede = (f'<div style="{S_LEDE}">今日精选 <strong>{total}</strong> 条内容，'
            f'按「项目机会库 / 增长运营 / 观点心法」分模块呈现。</div>')
    body = (f'<header style="{S_HEADER}"><div style="{S_KICKER}">AI 副业日报</div>'
            f'<h1 style="{S_H1}">副业日报 {_esc(date)}</h1>'
            f'<div style="{S_DATE}">{_esc(date)}（北京时间）· 自动生成</div></header>{lede}')
    for key, mtitle in MODULES:
        items = report.get("modules", {}).get(key, [])
        if not items:
            continue
        cards = "".join(render_item(it) for it in items)
        body += (f'<section style="{S_MOD}"><div style="{S_MHEAD}">'
                 f'<span style="{S_MTAG}"></span><h2 style="{S_MTITLE}">{mtitle}</h2>'
                 f'<span style="{S_MCOUNT}">精选 {len(items)}</span></div>'
                 f'<p style="{S_MSUB}">{_esc(MODULE_SUB.get(key, ""))}</p>{cards}</section>')
    ds = report.get("daily_summary", {})
    meth = ds.get("methodology", "")
    if meth:
        url_map = {}
        for k, _ in MODULES:
            for it in report.get("modules", {}).get(k, []):
                u = it.get("source_url")
                if u:
                    url_map[u] = (it.get("source_name", ""), it.get("title", ""))
        ev_parts = []
        for u in ds.get("evidence", []):
            sn, ti = url_map.get(u, ("", ""))
            ev_parts.append(f'<span style="{S_TEXT_LINK}">{_esc(ti or sn or _domain(u))}</span>')
        ev_html = " · ".join(ev_parts)
        body += (f'<section><div style="{S_SUMMARY}"><h2 style="{S_SUMMARY_H2}">每日总结 · 可复用方法论</h2>'
                 f'<div style="{S_METH}">{_esc(meth)}</div>'
                 f'<div style="{S_EV}">证据：{ev_html}</div></div></section>')
    html = (f'<!-- dr-renderer:{C.RENDERER_VERSION} -->'
            f'<div style="{S_WRAP}">{body}'
            f'<footer style="{S_FOOTER}">本文由流水线自动生成并推送至公众号。</footer></div>')
    return _strip_links(html)


def get_token(appid, secret):
    url = f"{WX_API}/token?grant_type=client_credential&appid={appid}&secret={secret}"
    r = requests.get(url, timeout=20, proxies=_WX_PROXIES)
    r.raise_for_status()
    d = r.json()
    if d.get("errcode"):
        raise RuntimeError(f"获取 access_token 失败: errcode={d.get('errcode')} errmsg={d.get('errmsg')}")
    return d["access_token"]


def _wx_call(method, path, token, json_body=None, files=None, timeout=60, retries=3):
    """微信接口调用：返回解析后的 json；非 0 errcode 抛出清晰错误。"""
    url = f"{WX_API}/{path}?access_token={token}"
    last_err = None
    for i in range(1, retries + 1):
        try:
            if files is not None:
                r = requests.post(url, files=files, timeout=timeout, proxies=_WX_PROXIES)
            else:
                r = requests.request(method, url, json=json_body, timeout=timeout, proxies=_WX_PROXIES)
            try:
                d = r.json()
            except Exception:
                r.raise_for_status()
                raise RuntimeError(f"{path} 返回非 JSON: {r.text[:200]}")
            if d.get("errcode", 0) not in (0, None):
                raise RuntimeError(f"{path} 失败: errcode={d.get('errcode')} errmsg={d.get('errmsg')}")
            return d
        except RuntimeError as e:
            last_err = e
            if i < retries:
                time.sleep(3 * i)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            if i < retries:
                time.sleep(3 * i)
    raise last_err or RuntimeError(f"{path} 调用失败")


def upload_image(token, path):
    with open(path, "rb") as f:
        files = {"media": (os.path.basename(path), f, "image/png")}
    d = _wx_call("POST", "material/add_material", token, files=files, timeout=90)
    return d["media_id"]


def add_news(token, article):
    d = _wx_call("POST", "material/add_news", token, json_body={"articles": [article]})
    return d["media_id"]


def draft_add(token, article):
    d = _wx_call("POST", "draft/add", token, json_body={"articles": [article]})
    return d["media_id"]


def mass_sendall(token, media_id, today):
    body = {
        "filter": {"is_to_all": True},
        "mpnews": {"media_id": media_id},
        "msgtype": "mpnews",
        "send_ignore": 0,
        "clientmsgid": f"fuyrribao-{today}",
    }
    return _wx_call("POST", "message/mass/sendall", token, json_body=body)


def freepublish(token, media_id):
    d = _wx_call("POST", "freepublish/submit", token, json_body={"media_id": media_id})
    return d.get("publish_id")


def _bj_now():
    """返回北京时间（显式时区，避免依赖 runner 系统 TZ）。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz)


def _load_accounts():
    """返回账号列表（dict）。支持 WX_ACCOUNTS(JSON) 与单账号兼容(WX_APPID)。"""
    raw = (os.environ.get("WX_ACCOUNTS") or "").strip()
    if raw:
        try:
            lst = json.loads(raw)
            if isinstance(lst, list) and lst:
                return lst
        except Exception as e:
            LOG.warning("WX_ACCOUNTS 解析失败（需 JSON 数组）：%s", e)
    appid = (os.environ.get("WX_APPID") or "").strip()
    secret = (os.environ.get("WX_APPSECRET") or "").strip()
    if appid and secret:
        return [{
            "name": "default",
            "appid": appid,
            "secret": secret,
            "mode": (os.environ.get("WX_PUBLISH_MODE") or "freepublish").strip().lower(),
            "default": True,
        }]
    return []


def _select_targets(accounts):
    """WX_PUSH_ALL=true → 全部；否则仅 default 账号（无 default 取首个）。"""
    push_all = (os.environ.get("WX_PUSH_ALL") or "").strip().lower() in ("1", "true", "yes")
    if push_all:
        return accounts
    for a in accounts:
        if a.get("default"):
            return [a]
    return [accounts[0]]


def _push_one(acc, report, today, now_bj):
    """对单个账号推送，返回实际使用的 mode 字符串。"""
    name = acc.get("name", "?")
    appid = (acc.get("appid") or "").strip()
    secret = (acc.get("secret") or "").strip()
    mode = (acc.get("mode") or "freepublish").strip().lower()
    author = acc.get("author") or os.environ.get("WX_AUTHOR", "副业日报")
    if not appid or not secret:
        raise RuntimeError("账号[%s] 缺少 appid/secret" % name)
    if mode not in ("mass", "freepublish", "draft"):
        raise RuntimeError("账号[%s] mode=%r 非法（应为 mass/freepublish/draft）" % (name, mode))

    # 封面（副业日报 + 日期，与标题一致）
    cover_path = os.path.join(C.DATA_DIR, "cover-%s.png" % today)
    make_cover(today, cover_path)

    token = get_token(appid, secret)
    cover_media_id = upload_image(token, cover_path)

    content = render_wechat(report)
    title = "副业日报 %s" % today  # 与封面一致
    digest = (report.get("daily_summary", {}).get("methodology") or "")[:64]
    article = {
        "title": title,
        "thumb_media_id": cover_media_id,
        "author": author,
        "digest": digest,
        "content": content,
        "content_source_url": "",
        "show_cover_pic": 1,
    }

    if mode == "mass":
        mid = add_news(token, article)
        res = mass_sendall(token, mid, today)
        LOG.info("账号[%s] 已群发（mass/sendall）: msg_id=%s", name, res.get("msg_id"))
        print("WECHAT_MSG_ID=" + str(res.get("msg_id", "")))
    elif mode == "draft":
        mid = draft_add(token, article)
        LOG.info("账号[%s] 已存草稿箱（draft/add）: media_id=%s —— 请后台点群发。", name, mid)
        print("WECHAT_DRAFT_MEDIA_ID=" + mid)
    else:
        mid = add_news(token, article)
        pid = freepublish(token, mid)
        LOG.info("账号[%s] 已发布（freepublish/submit）: publish_id=%s", name, pid)
        print("WECHAT_PUBLISH_ID=" + str(pid))
    return mode


def main():
    C.ensure_dirs()
    now_bj = _bj_now()
    today = now_bj.strftime("%Y-%m-%d")
    hour = now_bj.hour

    # ── 门控 1：未配置任何账号 → 仅发 WP，跳过公众号 ──
    accounts = _load_accounts()
    if not accounts:
        LOG.info("未配置公众号账号（WX_ACCOUNTS / WX_APPID），跳过公众号推送（仅发布 WordPress）。")
        return

    # ── 门控 2：仅当日末次触发（北京小时 >= WX_FINAL_HOUR，默认 19）才发，保证内容最全 ──
    final_hour = int((os.environ.get("WX_FINAL_HOUR", "19") or "19").strip() or "19")
    if hour < final_hour:
        LOG.info("当前北京小时=%d < 末次触发小时=%d，跳过公众号推送（待末次触发时再发当日最全版）。",
                 hour, final_hour)
        return

    # ── 选目标账号 ──
    targets = _select_targets(accounts)

    # ── 读日报数据 ──
    report = C.load_json(os.path.join(C.DATA_DIR, "report-%s.json" % today), {})
    if not report:
        LOG.error("无日报数据（report-%s.json 不存在），跳过推送。", today)
        raise SystemExit("no report data")
    total = sum(len(report.get("modules", {}).get(k, [])) for k, _ in MODULES)
    ds = report.get("daily_summary", {})
    if total == 0 and not ds.get("methodology"):
        LOG.info("今日无实质内容，跳过推送（不发布空文章）。")
        return

    # ── 门控 3：当日已推送过的账号跳过，绝不一天多篇 ──
    flag = os.path.join(C.STATE_DIR, "wechat_published_%s.json" % today)
    sent = {}
    if os.path.exists(flag):
        try:
            sent = json.load(open(flag, encoding="utf-8"))
        except Exception:
            sent = {}
    sent_names = set(sent.get("sent", []))

    done = []
    for acc in targets:
        nm = acc.get("name", "?")
        if nm in sent_names:
            LOG.info("账号[%s] 今日已推送，跳过重复。", nm)
            continue
        try:
            mode = _push_one(acc, report, today, now_bj)
            sent_names.add(nm)
            done.append("%s:%s" % (nm, mode))
            LOG.info("账号[%s] 推送完成（%s）。", nm, mode)
        except Exception as e:
            LOG.error("账号[%s] 推送失败: %s", nm, e)
            raise

    if done:
        try:
            json.dump({"sent": list(sent_names), "done": done, "at": now_bj.isoformat()},
                      open(flag, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception as e:
            LOG.warning("写入推送标记失败（不影响本次已推送）: %s", e)


if __name__ == "__main__":
    main()
