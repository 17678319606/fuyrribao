#!/usr/bin/env python3
"""周期主题合集（Item2）：从条目持久化 store 取近 N 天内容，由 AI 合成『主题周报』。

v2 重构（产品级重写）——把"周报"从「日报二次压缩的要点填空」升级为「有编辑主见、
有原文金句、有站内内链的主题策展产品」。

设计要点（对应产品方案）：
1. 选题与入选：先按评分地基挑出最值得策展的条目（信息密度/稀缺性/观点锐度/可行动性/
   来源权威），同主题重复只留最优表述（其余点评里一句带过）——【防冗余·策展空间比信息稀缺】。
2. 站内内链：发布前按主题召回本站点历史文章，周报正文/延展阅读必须自然插入 ≥3 条站内内链
   （锚文本自然化，如"我们上周拆解的那篇"）——【解决 13468 零内链 SEO 硬伤】。
3. 内容结构：钩子(hook) → 主题脉络/编辑地图 → 策展条目(金句+转述+第一人称点评三层)
   → 编辑手记 → 延展阅读——【围绕一个本周判断组织，而非罗列】。
4. 人情味/去套路：system prompt 固化"有态度的主理人"角色 + 显式禁用词清单 + 第一人称
   强制 + 句子错落 + 禁破折号堆砌——【突破 AI 固定套路硬伤】。
5. 安全：沿用原有 SHADOW/薄内容保护（条目不足跳过、HTML 过短跳过），任何异常不阻断发布。
"""
import os
import sys
import json
import time
import re
import base64
import html as html_lib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LOG = C.get_logger()

WINDOW_DAYS = int(os.environ.get("ROUNDUP_WINDOW_DAYS", "7"))
MIN_ITEMS = int(os.environ.get("ROUNDUP_MIN_ITEMS", "6"))
TOP_K = int(os.environ.get("ROUNDUP_TOP_K", "14"))        # 送 AI 前按评分保留的候选上限
MIN_ENTRIES = int(os.environ.get("ROUNDUP_MIN_ENTRIES", "5"))
REQUIRED_INTERNAL_LINKS = int(os.environ.get("ROUNDUP_INTERNAL_LINKS", "3"))

# —— 禁用词清单（命中即视为 AI 套路退化，用于输出后自检告警）——
BAN_WORDS = [
    "众所周知", "总而言之", "值得注意的是", "在当今社会", "在当今时代",
    "毋庸置疑", "综上所述", "此外", "另外", "与此同时", "值得一提的是",
    "在数字化转型的今天", "随着人工智能的发展",
]

SYSTEM_PROMPT = (
    "你是一位有态度、有烟火气的中文副业内容主理人，不是摘要机器。你给忙碌的普通人和"
    "中小老板写周报。文风铁律：\n"
    "① 全程第一人称，敢下判断、敢写『我觉得 / 我存疑 / 这周最让我意外的是』。\n"
    "② 禁用词（出现即判失败）：众所周知、总而言之、值得注意的是、在当今社会、在当今时代、"
    "毋庸置疑、综上所述、此外、另外、与此同时、值得一提的是、在数字化转型的今天、"
    "随着人工智能的发展。\n"
    "③ 句子长短剧烈错落，允许碎片句、允许以『和 / 但』开头；禁止破折号(——)堆砌。\n"
    "④ 开篇前两句必须用自己的声音抛出反常识或扎心观察，禁止『在…的今天』式开场。\n"
    "⑤ 每条策展必须含三层：其一，一句原文金句（从所给 source 的 summary/title 中提炼最"
    "有力的那句，带中文引号、署来源名并用链接标出）；其二，1-2 句你的转述（只补金句之外的"
    "背景，不重复金句）；其三，一句第一人称点评（敢褒贬、敢存疑）。\n"
    "⑥ 延展阅读里必须自然插入不少于 3 条『站内』历史文章链接，锚文本用自然说法"
    "（如『我们上周拆解的那篇』），绝不写『点击这里』。\n"
    "输出严格 JSON，不要任何解释、不要 markdown 围栏。"
)


def _esc(s):
    return html_lib.escape(str(s)) if s else ""


# 剥掉 AI 可能回带的编辑前缀，避免渲染时「主编按：主编按：…」双重前缀
_EDITOR_LABEL = re.compile(
    r"^\s*(主编按|编辑按|编辑手记|按|点评|评注|我的看法|一句话|补充)\s*[:：]?\s*")


def _strip_editor_label(s):
    return _EDITOR_LABEL.sub("", s or "").strip()


# ─────────────────────────────────────────────────────────────────────
# ① 评分地基：挑最优条目 + 同主题合并
# ─────────────────────────────────────────────────────────────────────
_AUTHORITY = {
    "github.com": 5, "news.ycombinator.com": 5, "lobste.rs": 5, "reddit.com": 4,
    "dev.to": 4, "producthunt.com": 4, "indiehackers.com": 4, "sspai.com": 4,
    "ruanyifeng.com": 5, "ifanr.com": 4, "w2solo.com": 5, "v2ex.com": 4,
    "jiqizhixin.com": 4, "qbitai.com": 4, "woshipm.com": 4, "geekpark.net": 4,
}
_SECOND = re.compile(r"你|您|我们|如何|可以|建议|步骤|先去|先做|第一步|实操|试试|不妨|方法|教程")
_ACTION = re.compile(r"融资|上线了|宣布|据悉|报道|表示|称|该(公司|团队|产品)")  # 第三人称被动→降权


def _host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) or "").lower() if m else ""


def _score_item(e):
    """0–5 综合分（轻量、零额外 LLM）。信息密度/稀缺性/观点锐度/可行动性/来源权威。"""
    summary = (e.get("summary", "") or "")
    title = (e.get("title", "") or "")
    src = e.get("source_url", "") or ""
    text = title + " " + summary
    # 信息密度：summary 长度 + 是否含具体数字
    density = 3
    if len(summary) >= 60:
        density += 1
    if re.search(r"\d", summary):
        density += 1
    # 可行动性：第二人称/实操信号
    actionable = 4 if (_SECOND.search(text) and len(summary) >= 40) else 2
    # 观点锐度（代理）：含立场/反直觉词或问句 → 加分；纯第三人称被动且无任何观点 → 降权
    sharp = 3
    viewpoint = re.search(r"别被|别再|坑|真相|警惕|其实|未必|存疑|我认为|我觉|对比|区别|vs", text)
    if viewpoint:
        sharp = 4
    if re.search(r"[？?]", title + summary):
        sharp = 5
    if _ACTION.search(text) and not viewpoint:
        sharp = 2
    # 来源权威
    authority = _AUTHORITY.get(_host(src), 3)
    score = (density + actionable + sharp + authority) / 4  # 四分量平均，归一到 0–5
    # 稀缺性（简单代理）：标题含具体工具/平台名 → 更独家
    if re.search(r"[A-Za-z]{2,}\.[a-z]{2,}|DeepSeek|Cursor|Claude|GPT|Meta|Google|小红书|抖音|视频号", title):
        score = min(5.0, score + 0.4)
    return round(min(5.0, score), 2)


def _cluster_key(title):
    t = html_lib.unescape((title or "").lower())
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    return t[:16] if len(t) >= 8 else ""


def _select(items):
    """评分排序 + 同主题合并（同 cluster 只留最高分），返回 TOP_K 候选。"""
    scored = []
    for e in items:
        e = dict(e)
        e["_score"] = _score_item(e)
        scored.append(e)
    scored.sort(key=lambda x: x["_score"], reverse=True)
    seen_clusters = {}
    out = []
    for e in scored:
        ck = _cluster_key(e.get("title", ""))
        if ck and ck in seen_clusters:
            # 同主题重复：已在的更高分保留，本条标记 merged（供 AI 在点评里一句带过）
            seen_clusters[ck].setdefault("_merged", []).append(e.get("title", ""))
            continue
        if ck:
            seen_clusters[ck] = e
        out.append(e)
        if len(out) >= TOP_K:
            break
    LOG.info("[周报·评分] 候选 %d → 去重合并后 %d 条送策展（TOP_K=%d）", len(items), len(out), TOP_K)
    return out


# ─────────────────────────────────────────────────────────────────────
# ② 站内内链召回（按【实体】多种子召回，而非仅模块名；并支持正文内联注入）
# ─────────────────────────────────────────────────────────────────────
_BRAND = re.compile(r"\b([A-Za-z][A-Za-z0-9.+]{1,20})\b")
# 已知产品/平台关键词（用于从标题抽取内链实体）
_KNOWN = ["DeepSeek", "Cursor", "Claude", "GPT", "Meta", "Google", "OpenAI",
          "小红书", "抖音", "视频号", "公众号", "WordPress", "Notion", "Figma",
          "Telegram", "YouTube", "GitHub", "ProductHunt"]


def _recall_internal_posts(terms, auth, base):
    """按多个实体关键词分别召回本站点历史文章（WP REST），去重后返回。失败返回 []。"""
    if not terms:
        return []
    try:
        import requests
        seen, links = set(), []
        for term in terms[:6]:
            if not term:
                continue
            try:
                url = base + "/wp-json/wp/v2/posts"
                params = {"search": term, "per_page": 5,
                          "_fields": "id,title,link", "status": "publish"}
                r = requests.get(url, params=params, headers=auth, timeout=15)
                r.raise_for_status()
                for p in r.json():
                    t = re.sub(r"<[^>]+>", "", p.get("title", {}).get("rendered", "")).strip()
                    link = p.get("link", "")
                    if t and link and link not in seen:
                        seen.add(link)
                        links.append({"text": t, "url": link, "term": term})
            except Exception as e:
                LOG.warning("[周报·内链] 关键词『%s』召回失败（跳过）：%s", term, e)
        LOG.info("[周报·内链] 共召回站内文章 %d 篇（关键词 %d 个）", len(links), len(terms))
        return links
    except Exception as e:
        LOG.warning("[周报·内链] 召回失败（不影响发布）：%s", e)
        return []


def _derive_seeds(items):
    """从候选条目抽取实体关键词（产品/公司名），用于内链召回。"""
    seeds = []
    for e in items:
        title = e.get("title", "") or ""
        for m in _BRAND.findall(title):
            ml = m.lower()
            if ml in ("ai", "api", "com", "app", "the", "and", "for", "v2", "v1", "url"):
                continue
            if m not in seeds:
                seeds.append(m)
        for k in _KNOWN:
            if k in title and k not in seeds:
                seeds.append(k)
    return seeds[:6]


def _inject_inline_links(html, posts):
    """把站内旧文以【实体】为锚，注入正文首次出现的未链接处（SEO 内联内链）。"""
    if not posts:
        return html
    try:
        for p in posts:
            anchor = p.get("text", "")
            url = p.get("url", "")
            if not anchor or not url or len(anchor) < 4:
                continue
            if 'href="%s"' % url in html:        # 已链接则跳过
                continue
            # 延展阅读区之前的标题出现才注入（避免破坏结构）
            idx = html.find(anchor)
            if idx == -1 or idx > html.find("延展阅读"):
                continue
            new = ('<a href="%s" target="_blank" rel="nofollow" '
                   'style="color:#2f6b5e;font-weight:600;">%s</a>'
                   % (_esc(url), _esc(anchor)))
            html = html[:idx] + new + html[idx + len(anchor):]
        return html
    except Exception as e:
        LOG.warning("[周报·内链] 内联注入失败（不影响发布）：%s", e)
        return html


def _derive_seed(items):
    """单主题词（给 AI prompt 作上下文，保留旧接口）。"""
    from collections import Counter
    mods = Counter(e.get("module", "") for e in items)
    top_mod = mods.most_common(1)
    mapping = {
        "project_opportunities": "副业项目",
        "growth_operations": "副业增长运营",
        "views_insights": "副业观点",
    }
    if top_mod:
        return mapping.get(top_mod[0][0], "副业")
    return "副业"


# ─────────────────────────────────────────────────────────────────────
# AI 调用
# ─────────────────────────────────────────────────────────────────────
def _ai_endpoints():
    """返回可用的 OpenAI 兼容端点列表（主用 → 兜底），任一可用即可。

    注意：Gemini 原生端点(generativelanguage.googleapis.com)与 /chat/completions
    不兼容，本脚本只走 OpenAI 兼容路径，故 roundup.yml 默认把璇玑(ai.jinbufenzi.com)
    设为主用端点。若日后要接 Gemini，需走原生 generateContent 路径（见 generate_report.py）。
    """
    eps = []
    b1 = (os.environ.get("ai_base_url", "") or os.environ.get("AI_BASE_URL", "")).rstrip("/")
    k1 = (os.environ.get("AI_API_KEY", "") or os.environ.get("ai_api_key", "")
          or os.environ.get("AI_SIDEHUSTLE_API_KEY", "")).strip()
    m1 = (os.environ.get("AI_MODEL", "") or os.environ.get("ai_model", "") or "auto").strip()
    if b1 and k1:
        eps.append((b1, k1, m1))
    b2 = (os.environ.get("AI_FALLBACK_URL", "")).rstrip("/")
    k2 = (os.environ.get("AI_FALLBACK_KEY", "")).strip()
    m2 = (os.environ.get("AI_FALLBACK_MODEL", "") or "auto").strip()
    if b2 and k2:
        eps.append((b2, k2, m2))
    if not eps:
        raise RuntimeError("未配置 AI 端点(AI_BASE_URL/AI_API_KEY 或 AI_FALLBACK_*)")
    return eps


def _call_ai(system, user):
    """调用 AI 端点合成周报，带健壮重试（治根因：run3 整期周报因璇玑瞬时连接错而失败）。

    重试策略：
      - 传输层错误（ConnectionError / Timeout / 网络抖动 / urllib3 MaxRetryError）：
        属瞬时故障，指数退避后重试（本端点最多 MAX_TRIES 次，仍失败再换兜底端点）。
      - HTTP 429 / 5xx：限流或服务端抖动，退避后重试。
      - HTTP 4xx（非 429，如 400/401/403）：配置/鉴权错误，本端点无望，直接换下一个兜底端点
        （不浪费重试额度）。
    任一端点成功即返回；全部端点耗尽才抛出 last。
    """
    import requests
    from requests.exceptions import HTTPError, ConnectionError as ReqConnectionError, Timeout, RequestException
    # 统一限速器：非 Gemini 端点仍做 RPM 滑窗限速（不计 Gemini 免费预算），
    # 避免周报密集调用撞上游 429；周报低频，限速足够温和。
    C.ai_limiter.throttle(is_gemini=False)
    eps = _ai_endpoints()
    last = None
    MAX_TRIES = 4
    for (base, key, model) in eps:
        url = base + "/chat/completions"
        auth = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.85,
        }
        for i in range(1, MAX_TRIES + 1):
            try:
                r = requests.post(url, headers=auth, json=body, timeout=150)
                sc = r.status_code
                if sc == 429 or sc >= 500:
                    wait = min(30, 5 * i)
                    LOG.warning("AI[%s] 返回 %s，%ds 后重试(%d/%d)", base, sc, wait, i, MAX_TRIES)
                    time.sleep(wait)
                    last = RuntimeError("ai %s" % sc)
                    continue
                if 400 <= sc < 500:
                    # 客户端错误（鉴权/配置）本端点无望，直接换兜底端点
                    last = RuntimeError("ai %s (client error)" % sc)
                    LOG.warning("AI[%s] 客户端错误 %s（配置/鉴权问题，换兜底端点）", base, sc)
                    break
                return r.json()["choices"][0]["message"]["content"]
            except HTTPError as e:
                last = e
                LOG.warning("AI[%s] HTTP 错误 %s，换兜底端点", base, e)
                break
            except (ReqConnectionError, Timeout, RequestException) as e:
                # 瞬时传输错误：指数退避后重试（跨端点兜底前尽量自救）
                wait = min(30, 3 * (2 ** (i - 1)))
                LOG.warning("AI[%s] 网络/连接错误(%s)，%ds 后重试(%d/%d)",
                            base, type(e).__name__, wait, i, MAX_TRIES)
                last = e
                time.sleep(wait)
        LOG.warning("AI 端点 %s 失败，尝试下一个兜底端点", base)
    raise last or RuntimeError("ai call failed")


def _extract_json(text):
    """从模型输出抠出 JSON（兼容 ``` 围栏 / 前后废话 / 截断）。

    原先的 r'\\{.*\\}' 贪婪匹配在遇到围栏或尾随文字时会把多余内容一起塞进
    json.loads，触发 'Extra data' 导致整期周报合成失败。这里改为：剥离代码
    围栏 → 从首个 { 扫描到与之配平的 }（尊重字符串内引号/转义）→ 优先解析该
    完整对象；未闭合时退而求其次截到最后一个 }。与日报侧 _extract_json 对齐。"""
    if not text:
        raise ValueError("AI 未返回内容")
    s = text.strip()
    if "```" in s:  # 剥离任意语言标记围栏（```json / ```python / 裸 ```）
        s = re.sub(r"```[a-zA-Z]*\s*", "", s, flags=re.I)
        s = s.replace("```", "")
    s = s.strip()
    start = s.find("{")
    if start == -1:
        raise ValueError("AI 未返回 JSON")
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
    if end == -1:  # 未闭合：退而求其次截到最后一个 }
        last = s.rfind("}")
        if last == -1:
            raise ValueError("AI 返回 JSON 未闭合")
        end = last
    candidate = s[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", candidate, re.S)  # 再兜底一次
        if m:
            return json.loads(m.group(0))
        raise


# ─────────────────────────────────────────────────────────────────────
# ③ 渲染（新骨架）
# ─────────────────────────────────────────────────────────────────────
def _render_roundup(data, today):
    theme = _esc(data.get("theme") or "副业主题周报")
    hook = data.get("hook") or ""
    mp = data.get("map") or ""
    entries = data.get("entries") or []
    notes = data.get("notes") or ""
    readmore = data.get("readmore") or []

    S = (
        "max-width:760px;width:100%;margin:0 auto!important;"
        "padding:clamp(18px,5vw,36px) clamp(12px,4vw,24px) clamp(36px,8vw,64px)!important;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif!important;"
        "line-height:1.9!important;color:#2b2b2b!important;background:#faf9f7!important;"
        "box-sizing:border-box!important;"
    )
    H1 = "font-size:clamp(22px,6vw,30px);font-weight:900;color:#1f1f1f;margin:6px 0 4px;"
    DATE = "color:#8a8a8a;font-size:14px;"
    LEAD = ("font-size:17px;font-weight:600;color:#333;background:#fffaf2;"
            "border-left:4px solid #e0a93b;padding:14px 18px;margin:18px 0 26px;border-radius:0 10px 10px 0;")
    MAP = ("background:#fff;border:1px solid #e6e3dd;border-left:3px solid #2f6b5e;"
           "border-radius:12px;padding:16px 20px;margin:22px 0;font-size:15.5px;color:#3a3a3a;")
    Q = ("margin:14px 0 6px;padding:10px 16px;background:#f4f7f6;border-left:4px solid #2f6b5e;"
         "border-radius:0 8px 8px 0;font-size:16px;color:#1f3a34;font-style:normal;")
    COMMENT = "margin:4px 0 18px;color:#7a5a2a;font-size:15px;"
    NOTE = ("margin:26px 0;padding:18px 20px;background:#fffaf2;border:1px dashed #e0a93b;"
            "border-radius:12px;font-size:15.5px;color:#4a3a1a;")
    LINK_INTERNAL = "color:#2f6b5e;font-weight:600;"
    LINK_EXT = "color:#8a6d3b;"

    parts = [f'<div style="{S}">']
    parts.append('<div style="text-align:center;padding-bottom:18px;border-bottom:2px solid #e6e3dd;margin-bottom:24px;">'
                 f'<div style="color:#2f6b5e;font-weight:700;font-size:12px;letter-spacing:2px;">副业主题周报</div>'
                 f'<h1 style="{H1}">{theme}</h1>'
                 f'<div style="{DATE}">{today}（北京时间）· AI 驱动生成</div></div>')

    if hook:
        parts.append(f'<div style="{LEAD}">{_esc(hook)}</div>')
    if mp:
        parts.append(f'<div style="{MAP}"><b>本周编辑地图</b><br>{_esc(mp)}</div>')

    for en in entries:
        quote = (en.get("quote") or "").strip().strip('"').strip('"')
        src = _esc(en.get("source") or "")
        surl = (en.get("source_url") or "").strip()
        para = (en.get("paraphrase") or "").strip()
        comment = _strip_editor_label(en.get("comment") or "")
        parts.append('<div style="margin:22px 0;">')
        if quote:
            if surl:
                parts.append(f'<div style="{Q}">“{_esc(quote)}”'
                             f' —— <a href="{_esc(surl)}" target="_blank" rel="nofollow">{src}</a></div>')
            else:
                parts.append(f'<div style="{Q}">“{_esc(quote)}” —— {src}</div>')
        if para:
            parts.append(f'<p style="margin:6px 0;">{_esc(para)}</p>')
        if comment:
            parts.append(f'<p style="{COMMENT}">主编按：{_esc(comment)}</p>')
        parts.append('</div>')

    if notes:
        parts.append(f'<div style="{NOTE}"><b>编辑手记</b><br>{_esc(notes)}</div>')

    if readmore:
        parts.append('<h2 style="font-size:18px;margin:30px 0 12px;color:#1f1f1f;">延展阅读</h2><ul style="padding-left:20px;line-height:2;">')
        for rm in readmore:
            t = _esc(rm.get("text") or "")
            u = _esc(rm.get("url") or "")
            is_internal = "dajiayouxuan.com" in u
            style = LINK_INTERNAL if is_internal else LINK_EXT
            tag = '<span style="font-size:11px;color:#2f6b5e;border:1px solid #2f6b5e;border-radius:4px;padding:0 4px;margin-right:4px;">站内</span>' if is_internal else ""
            if u:
                parts.append(f'<li>{tag}<a href="{u}" target="_blank" style="{style}">{t}</a></li>')
            else:
                parts.append(f'<li>{t}</li>')
        parts.append('</ul>')

    parts.append('</div>')
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────
# R2 聚合源强制原文引用：aggregator 类源（如 zhongnianren）好观点多，
# 但若只转述不引原文，读者看不到一手论据 → 强制 quote 非空 + source_url 指向一手源。
# ─────────────────────────────────────────────────────────────────────
AGGREGATOR_SOURCES = C.AGGREGATOR_SOURCE_IDS | C.AGGREGATOR_NAME_ALIASES  # 仅作引用/向后兼容


def _fetch_post_excerpt(url, auth, max_chars=140):
    """取文章首段有意义的摘录，用于聚合源强制引用兜底（无凭据/失败返回空）。"""
    if not url:
        return ""
    try:
        import requests
        r = requests.get(url, headers=auth, timeout=20)
        r.raise_for_status()
        text = re.sub(r"<[^>]+>", "", r.text or "")
        text = re.sub(r"\s+", " ", text).strip()
        for seg in re.split(r"[。！？\n]", text):
            seg = seg.strip()
            if len(seg) >= 20 and any("\u4e00" <= c <= "\u9fff" for c in seg):
                return seg[:max_chars]
    except Exception:
        pass
    return ""


def _enforce_aggregator_citation(entries, wp_auth, wp_url):
    """对 aggregator 源条目强制携带原文引用：quote 非空（缺失则回退抓取原文首段），
    source_url 指向一手源（缺失则退回 report_link）。返回处理后的 entries。"""
    for e in entries:
        src = (e.get("source_name") or "").strip().lower()
        if not C.is_aggregator_source(src):
            continue
        quote = (e.get("quote") or "").strip()
        if not quote:
            surl = (e.get("source_url") or (e.get("report_link") or "")).strip()
            excerpt = _fetch_post_excerpt(surl, wp_auth)
            if excerpt:
                e["quote"] = excerpt
                LOG.info("【聚合源强制引用】%s 缺 quote，已回退抓取原文首段填充", src)
            else:
                # 强制引用兜底仍失败：标记 + 升级为 ERROR，避免无出处聚合内容被当作原创呈现（抄袭风险）。
                e["_citation_missing"] = True
                LOG.error("【聚合源强制引用】%s 仍缺 quote 且无法回退抓取（已标记 _citation_missing，需人工核查出处）", src)
        if not (e.get("source_url") or "").strip() and (e.get("report_link") or "").strip():
            e["source_url"] = e["report_link"]
    return entries


def _ban_check(html_text):
    hits = [w for w in BAN_WORDS if w in html_text]
    if hits:
        LOG.warning("[周报·自检] 检出疑似套路词：%s（建议复核）", "、".join(hits))
    return hits


# ─────────────────────────────────────────────────────────────────────
# 发布
# ─────────────────────────────────────────────────────────────────────
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

    # —— 读取 store + 条目级去重 ——
    path = os.path.join(C.STATE_DIR, "items_store.jsonl")
    if not os.path.exists(path):
        LOG.info("items_store 不存在，跳过。")
        return
    cutoff = C.days_ago_iso(WINDOW_DAYS)[:10]
    raw = []
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
                raw.append(e)
    if len(raw) < MIN_ITEMS:
        LOG.info("近 %d 天条目仅 %d 条（<%d），跳过本期合集，避免薄内容。",
                 WINDOW_DAYS, len(raw), MIN_ITEMS)
        return

    seen = set()
    cleaned = []
    for e in raw:
        k = e.get("source_url") or e.get("title")
        if k in seen:
            continue
        seen.add(k)
        cleaned.append(e)

    # —— ① 评分挑优 + 同主题合并 ——
    selected = _select(cleaned)
    if len(selected) < MIN_ENTRIES:
        LOG.info("评分入选仅 %d 条（<%d），跳过避免薄内容。", len(selected), MIN_ENTRIES)
        return

    brief = [{
        "date": e.get("date", ""),
        "module": e.get("module", ""),
        "title": e.get("title", ""),
        "source_name": e.get("source_name", ""),
        "source_url": e.get("source_url", ""),
        "report_link": e.get("report_link", ""),
        "summary": (e.get("summary", "") or "")[:220],
        "score": e.get("_score", 0),
    } for e in selected]
    brief_json = json.dumps(brief, ensure_ascii=False)

    # —— ② 站内内链召回 ——
    wp_url = os.environ.get("WP_URL", "https://dajiayouxuan.com").rstrip("/")
    user = os.environ.get("WP_USER", "tougao")
    app_pw = os.environ.get("WP_APP_PASSWORD", "")
    wp_auth = {"Authorization": "Basic " + base64.b64encode(
        f"{user}:{app_pw}".encode()).decode(), "Content-Type": "application/json"}
    seeds = _derive_seeds(selected)
    if not seeds:
        seeds = [_derive_seed(selected)]
    internal = _recall_internal_posts(seeds, wp_auth, wp_url)
    internal_json = json.dumps(internal, ensure_ascii=False)

    prompt = (
        "下面是近 %d 天『副业日报』沉淀、并已按质量评分挑出的候选条目（JSON 数组，"
        "每条含 date/module/title/source_name/source_url/report_link/summary/score）。\n"
        "同时附上【站内历史文章】internal_links——这是你自己站点的旧文。周报里必须：\n"
        "  (a) 在正文提到相关产品/观点的地方，自然插入内联站内链接（锚文本用自然说法，"
        "如『我们上周拆解的那篇』，禁止『点击这里』）；\n"
        "  (b) 在末尾【延展阅读】里不少于 %d 条取自 internal_links（站内，标 is_internal=true）。\n"
        "两处合计站内内链要充足（延展阅读已含不少于上数的站内旧文）。\n\n"
        "任务：先按『信息密度/稀缺性/观点锐度/可行动性』从候选里精选 6-9 条最值得策展的，"
        "同主题若仍有重复只留最优表述（其余可在点评里一句带过）。写一篇约 900-1300 字的中文"
        "『主题周报』。\n\n"
        "硬性结构（必须全部出现，按顺序）：\n"
        "1) hook：50-100字，反常识/扎心开场，第一人称，禁止『在…的今天』式。\n"
        "2) map：150-300字『本周编辑地图』，你对本周主题的独家梳理（发生了什么/为什么重要/"
        "三条暗线），零引用也要有你的主见。\n"
        "3) entries：6-9 条策展，每条含 quote(从 source 的 summary/title 提炼最有力的一句原话，"
        "带中文引号、署 source_name 并在 source_url 标链接)、paraphrase(1-2句只补背景)、"
        "comment(第一人称点评，敢褒贬)。\n"
        "4) notes：200-400字『编辑手记』，收尾下判断，敢说『这周大部分报道夸大了X』，"
        "可承认复杂感受。\n"
        "5) readmore：3-5 条链接，其中不少于 %d 条取自 internal_links（站内，标 is_internal=true），"
        "其余用 report_link（相关日报）或 source_url（一手源），is_internal=false。\n\n"
        "严格输出 JSON（双引号）：\n"
        "{\"theme\":\"主题名(≤12字)\",\"hook\":\"...\",\"map\":\"...\","
        "\"entries\":[{\"quote\":\"\",\"source_name\":\"\",\"source_url\":\"\",\"paraphrase\":\"\",\"comment\":\"\"}],"
        "\"notes\":\"...\",\"readmore\":[{\"text\":\"锚文本\",\"url\":\"\",\"is_internal\":true}]}\n\n"
        "候选条目：\n%s\n\n站内历史文章：\n%s"
    ) % (WINDOW_DAYS, REQUIRED_INTERNAL_LINKS, REQUIRED_INTERNAL_LINKS, brief_json, internal_json)

    try:
        raw_ai = _call_ai(SYSTEM_PROMPT, prompt)
        data = _extract_json(raw_ai)
        # R2 聚合源强制原文引用：确保 zhongnianren 等 aggregator 条目带原文引用
        entries = data.get("entries") or []
        entries = _enforce_aggregator_citation(entries, wp_auth, wp_url)
        data["entries"] = entries
        html = _render_roundup(data, today)
        html = _inject_inline_links(html, internal)  # 正文内联站内内链（SEO）
    except Exception as e:
        LOG.error("AI 合成合集失败，跳过发布：%s", e)
        return

    if len(html) < 600:
        LOG.warning("合集 HTML 过短(%d 字)，疑似 AI 退化，跳过发布避免薄内容。", len(html))
        return

    # 条目金句自检：生死线
    entries = data.get("entries") or []
    quoted = [e for e in entries if (e.get("quote") or "").strip()]
    if len(quoted) < max(3, len(entries) // 2):
        LOG.warning("合集金句覆盖率低（%d/%d），疑似退化，仍发布但已记录。", len(quoted), len(entries))
    _ban_check(html)

    # 站内内链自检
    internal_used = sum(1 for rm in (data.get("readmore") or [])
                        if rm.get("is_internal") and "dajiayouxuan.com" in (rm.get("url") or ""))
    LOG.info("[周报·自检] 站内内链实际 %d 条（要求≥%d）", internal_used, REQUIRED_INTERNAL_LINKS)

    theme = (data.get("theme") or "副业主题周报").strip()
    title = f"副业主题周报 · {theme} · {today}"
    cat_name = os.environ.get("WP_ROUNDUP_CATEGORY", "主题合集")
    try:
        pid, link = _publish(wp_url, wp_auth, title, html, cat_name)
        LOG.info("已发布主题周报(post=%s): %s", pid, link)
        print("ROUNDUP_URL=" + link)
    except Exception as e:
        LOG.error("合集发布失败：%s", e)
        raise SystemExit("roundup publish failed")


if __name__ == "__main__":
    main()


