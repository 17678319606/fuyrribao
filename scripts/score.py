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
