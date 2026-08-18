#!/usr/bin/env python3
"""内容质量打分地基（通用层 + 主题层配置）。

设计目标（与 docs/source-standard.md §6 呼应）：
  - 通用层：与主题无关的信号——权威域 / 行动性(第二人称可操作 vs 第三人称被动) /
    时效 / 空心检查。可覆盖 ~75-85% 的量化信号，换主题零改动。
  - 主题层：从 themes/<theme>.json 读取 junk_categories / quality_signals / 权重
    （副业专属规则抽离，便于迁移到新主题日报）。
  - 当前为 SHADOW 模式：只计算并 LOG 分数分布 + 低分候选，绝不拦截（避免误杀好内容）。
    待 shadow 数据积累、精确率≥80% 后，再开启 active_intercept 做主动过滤。

稳定性：本模块被 generate_report 以 try/except 包裹调用，任何异常都不影响发布。
"""
import os
import re
import json
import datetime

import common as C

# —— 通用权威域分级（与 discover_sources 同源；未来可迁到 common 单一真源）——
_AUTHORITY = {
    "github.com": 5, "reddit.com": 4, "news.ycombinator.com": 5, "lobste.rs": 5,
    "dev.to": 4, "producthunt.com": 4, "indiehackers.com": 4, "sspai.com": 4,
    "ruanyifeng.com": 5, "ifanr.com": 4, "oschina.net": 4, "qbitai.com": 4,
    "jiqizhixin.com": 4, "geekpark.net": 4, "woshipm.com": 4, "growthhackers.com": 4,
    "appinn.com": 4, "v2ex.com": 4, "w2solo.com": 5, "smashingmagazine.com": 5,
}

# 第二人称可操作信号（好内容常含"你/如何/步骤"）
_SECOND = re.compile(r"你|您|我们|如何|可以|建议|步骤|先去|先做|第一步|实操|试试|不妨")
# 第三人称被动罗列（垃圾共性：他/该团队/据悉/报道/表示）
_THIRD = re.compile(r"他|她|该(公司|团队|产品|项目|平台)|据悉|报道|表示|称|近日|融资|宣布|上线了")


def _host(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) or "").lower() if m else ""


def _authority(url):
    h = _host(url)
    for d, s in _AUTHORITY.items():
        if d in h:
            return s
    return 3


def load_theme(theme="sidehustle"):
    path = os.path.join(C.REPO_ROOT, "themes", theme + ".json")
    return C.load_json(path, {})


def generic_score(item):
    """通用层单条打分（0–5 区间各维度）。不依赖主题。"""
    title = item.get("title", "") or ""
    sig = item.get("signal", "") or ""
    mvp = item.get("how_to_mvp", "") or ""
    acq = item.get("acquisition_channel", "") or ""
    mon = item.get("monetization", "") or ""
    src = item.get("source_url", "") or ""
    text = title + " " + sig + " " + mvp + " " + acq + " " + mon

    # 行动性：第二人称可操作 vs 第三人称被动
    sec = len(_SECOND.findall(text))
    act_len = len(mvp.strip()) + len(acq.strip()) + len(mon.strip())
    if sec >= 1 and act_len >= 20:
        actionable = 5
    elif sec >= 1:
        actionable = 3
    else:
        actionable = 1

    authority = _authority(src)

    # 时效（SHADOW 阶段：用 published_at 粗略计算，无日期默认中等）
    recency = 3

    # 空心：核心行动字段是否缺失（结构性兜底思路，与 generate_report._is_hollow_item 同源不同实现）
    hollow = 1 if (len(mvp.strip()) < 10 and len(acq.strip()) < 10 and len(mon.strip()) < 10) else 5

    composite = round((authority + actionable + recency + hollow) / 4 * 5)  # 归一到 0–5
    return {"authority": authority, "actionable": actionable, "recency": recency,
            "hollow_check": hollow, "composite": composite}


def shadow_score_report(report, theme="sidehustle"):
    """SHADOW：汇总打分分布并 LOG，不拦截。"""
    LOG = C.get_logger()
    theme_cfg = load_theme(theme)
    mods = report.get("modules", {})
    all_scores = []
    low = []
    for mod, items in mods.items():
        if not isinstance(items, list):
            continue
        for it in items:
            s = generic_score(it)
            all_scores.append(s["composite"])
            if s["composite"] <= 2:
                low.append((mod, (it.get("title", "") or "")[:40], s["composite"]))
    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        LOG.info("[打分地基·SHADOW] 主题=%s 条目 %d，平均分 %.2f，低分(≤2) %d 条（仅记录，不拦截）",
                 theme, len(all_scores), avg, len(low))
        for mod, t, c in low[:10]:
            LOG.info("  低分候选: [%s] %s (composite=%d)", mod, t, c)
    else:
        LOG.info("[打分地基·SHADOW] 主题=%s 无条目可评", theme)
    return {"avg": (sum(all_scores) / len(all_scores)) if all_scores else 0,
            "count": len(all_scores), "low": len(low)}


# —— 实质含量门禁（v5.0，激活拦截：过滤"无数据经验主义 + 新闻通告"）——
# 动机：SHADOW 评分只记日志不拦截，导致「观点心法」模块塞满零数据零案例的纯主观
# 心法、以及行业新闻复述。本检测器以"实质锚点"为核心判据：
#   实质锚点 = ① 具体数字/指标 ② 具体可操作框架/案例（步骤/SOP/亲历实验）
# 一条内容若没有任何实质锚点，仅是主观总结/新闻通告 → 判定为废经验，拦截。
# 设计原则（与"按共性筛、不打补丁"铁律一致）：用【语义共性模式】而非关键词黑名单，
# 关键词仅作为"触发信号"，最终裁决看"是否含实质锚点"，避免误杀带数据/案例的好内容。

# 新闻通告动词（某公司推出/上线/开源/涨价…）：纯复述公告，无分析即废
_NEWS_VERB = re.compile(
    r"推出|上线了?|发布|开源|涨价|联手|合作|收购|融资|宣布|上新|更名|获批|"
    r"登录(港股|美股|a股|新三板)")
# 分析动词（若新闻同时含分析角度，则视为策展而非复述，放行）
_ANALYSIS_VERB = re.compile(
    r"影响|策略|应对|意味着|建议|解读|对比|为什么|为何|怎么|分析|启示|风险|"
    r"机会|看待|思考|复盘|警惕|坑|教训|如何避|怎么避|凭什么|底层逻辑")
# 空洞 How-to 触发（标题含"如何/怎么/为何"且正文无数字无框架）
_HOWTO = re.compile(r"如何|怎么|为何|怎样|how\s*to", re.IGNORECASE)
# 虚假"实测/体验"claim：标题吹实测/实战但正文无任何数字结果
_FAKE_REVIEW = re.compile(r"实测|实战|亲测|上手|体验|评测|深度体验")
# 具体数字/指标（带单位或区间）：12倍 / 5万星 / 195次 / 1k→12k / 30分钟 / 219个包 …
_NUMBER = re.compile(
    r"\d+\s*(?:\.\d+)?\s*"
    r"(?:%|％|倍|万|亿|千|个|条|次|封|小时|分钟|天|周|元|美元|美金|\$|￥|星|℃|"
    r"px|kb|mb|gb|tb|w\b|k\b)"
    r"|\d+\s*[kKwWmM]?\s*[→\-~～]\s*\d+")
# 具体可操作框架/案例/能力描述：步骤/SOP/亲历实验/具体做法/方法论，
# 也覆盖"工具具体能力"表述（支持/转/操作/重构/引流/三库/3步…），避免误杀合格工具条目。
# 注意：「先X再Y」必须接具体动作动词（验证/做/搭/写/建…），否则"先理需求再分析"这类
# 通用连接词会被误判为框架——那不是实质锚点。
_TACTIC = re.compile(
    r"第一步|先(验证|梳理|做|搭|写|建|测|调|跑|试|列|画|设|配|看|准备|部署|接入)[^，。]{0,12}再|"
    r"具体做法|流程|步骤|实操|SOP|框架|方法论|真实?案例|具体案例|案例[:：]|举个?案例|"
    r"我用|我们做了|我试了|亲历|踩坑|具体怎么|做法是|做法就是|"
    r"比如[^，。]{0,20}(做|搭|写|建|涨|赚|测)|"
    r"3步|三步|三库|"
    r"支持|提供|内置|集成|重构|实现|操作|处理|解决|"
    r"可(直接)?(控|生成|转|搭|建|调)|"
    r"降[低本]|提[升高]|增[长加]|保[^，。]{0,6}稳定|引流|冷启")
# 具名实体（知名产品/公司/人物/工具）：作为"提及了具体事物"的弱锚点
_ENTITY_LIST = [
    "DeepSeek", "GLM", "Codex", "Claude", "Motrix", "SaaStr", "Murf", "Falcon",
    "Telegram", "YouTube", "Django", "RAG", "LLM", "SaaS", "GEO", "SEO", "MCP",
    "Gemini", "OpenAI", "Apple", "阿里", "百度", "苹果", "腾讯", "字节", "华为",
    "猿辅导", "妙多", "Harness", "Cordis", "Cursor", "GitHub", "ProductHunt",
    "HackerNews", "V2EX", "微信", "抖音", "小红书", "公众号", "TG", "Genspark",
    "GenOffice", "WorkBuddy", "SoftCircle", "BrowserAct", "Caveman", "MoneyBuddy",
    "Saathi", "Song Finder", "Reasonix", "DeepSeek V4", "GLM 5.3",
]
_ENTITY = re.compile(r"(?:%s)" % "|".join(re.escape(e) for e in _ENTITY_LIST))


def substance_classify(item, module=None, exempt_views=False):
    """判定一条 item 是否"废经验/新闻通告"（应拦截）。

    返回 dict: {drop, reasons, has_num, has_entity, has_tactic, strong, substance}
    - substance：实质锚点计数（num/tactic 各算强锚点；entity 仅算弱锚点 0.5）
    - 拦截规则（任一命中即 drop）：
      ① 新闻复述：含新闻动词 + 具名实体 + 无分析动词 + 无数字 → 纯公告复述
      ② 虚假实测：标题含实测/实战等但无数字结果
      ③ 空洞 How-to：标题含如何/怎么/为何 + 无数字 + 无框架
      ④ 观点模块弱内容：module==views_insights 且 既无数字也无框架（纯主观心法）
    - exempt_views：True 时跳过规则④。用于聚合源(zhongnianren)的人情味观点——
      这类内容的价值本就是"非数据的真实经验洞察"，不应被"无数字即废"误杀（否则
      "去 AI 味"反被质量门反噬）。新闻复述/虚假实测/空洞 How-to 仍照常拦截。
    注意：本函数只判定、不修改 item；是否真正丢弃由调用方决定（保持地基"纯评估"定位）。
    """
    title = (item.get("title") or "")
    parts = [title]
    for k in ("signal", "perspective", "value_proposition", "how_to_mvp",
              "acquisition_channel", "monetization", "replicability",
              "summary", "content", "target_customer"):
        v = item.get(k)
        if v:
            parts.append(str(v))
    text = " ".join(parts)

    has_num = bool(_NUMBER.search(text))
    has_entity = bool(_ENTITY.search(text))
    has_tactic = bool(_TACTIC.search(text))
    # 强锚点 = 数字 或 具体框架/案例/能力。具名实体(公司/工具名) alone 不算锚点——
    # 专名救不了"无数据无案例"的废经验（如"猿辅导前妙多研发负责人想…"仍无实质）。
    strong = has_num or has_tactic

    head = title + " " + text[:260]
    # 新闻复述：具名实体 + 无分析 + 无具体能力，且「无数字 或 位于观点模块」才判纯复述。
    # —— 项目/增长模块的带数字发布（如"上线12小时5万星"）保留；观点模块的纯新闻复述（如
    # "DeepSeek涨价12倍"即便带数字）一律拦截，因为观点模块应是策展洞察而非行业通讯。
    is_news = bool(_NEWS_VERB.search(head)) and has_entity \
        and not _ANALYSIS_VERB.search(text) and not has_tactic \
        and (not has_num or module == "views_insights")
    is_fake_review = bool(_FAKE_REVIEW.search(title)) and not has_num and not has_tactic
    is_empty_howto = bool(_HOWTO.search(title)) and not has_num and not has_tactic
    # 观点模块弱内容：既无数字也无真实框架/案例 → 纯主观心法，拦截
    # 但聚合源(zhongnianren)的人情味洞察豁免本规则（exempt_views），仅拦截其余废条目。
    is_views_weak = (module == "views_insights") and not strong and not exempt_views

    reasons = []
    if is_news:
        reasons.append("新闻通告式复述(无分析无数据)")
    if is_fake_review:
        reasons.append("标题吹实测/实战但无数字结果(虚假实测)")
    if is_empty_howto:
        reasons.append("空洞How-to(无数据无框架)")
    if is_views_weak:
        reasons.append("观点模块无实质锚点(无数据无案例无框架)")

    substance = (1 if has_num else 0) + (1 if has_tactic else 0) \
        + (0.5 if (has_entity and not strong) else 0)
    return {
        "drop": bool(reasons), "reasons": reasons,
        "has_num": has_num, "has_entity": has_entity,
        "has_tactic": has_tactic, "strong": strong, "substance": substance,
    }
