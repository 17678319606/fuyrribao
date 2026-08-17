# scripts/ad_filter.py
"""硬安全闸：代码侧零 LLM 成本的广告/博彩/引流/违法内容过滤器。
必须在 AI 调用前、且在 zhongnianren exempt_views（人情味豁免）之前无条件运行（双闸）。

设计原则：
1. 安全 > 人情味：任何源的博彩/引流/违法内容都不因"聚合源豁免"被发布。
2. 零 LLM 成本：纯正则，跑在送 AI 前与生成后，反而因提前丢垃圾而省 token。
3. 双闸纵深防御：① 送 AI 前对 raw 信号过闸；② 生成后对 report JSON 过闸。
4. 高精低误杀：正则按违规特征分组，仅匹配明确违规，避免误伤正常搞钱内容。
5. 可单测：纯函数，喂线上真实样本做回归断言。
"""
import re

# —— 博彩 / 赌博 / 黑产（高精，绝不豁免）——
_GAMBLING = re.compile(
    r"(博彩|六合彩|49倍|加拿大28|加拿大二八|PG电子|PG娱乐|娱乐城|贵宾会|百家乐|"
    r"外围盘|外围赌|网赌|赌球|赌马|开奖|返水|上押|黑台|野鸡|菠菜|棋牌|投注|下注|"
    r"铂莱|750\.cc|bet\.|casino|gamble|博彩信誉|担保上押)",
    re.IGNORECASE,
)
# —— 引流 / 联系方式（高精）——
# 注意：刻意不收录裸「二维码」——赞赏区「扫描二维码」「微信赞赏码」属正常内容，
# 误杀会污染 QA；二维码单独出现无法判定为引流，故仅匹配明确的加好友/加群话术。
_PROMO_CONTACT = re.compile(
    r"(加微信|微信号|微信：|薇信|扫码关注|扫码免费领|私聊客服|小妹|客服微信|"
    r"t\.me/|电报群|TG群|telegram\.me)",
    re.IGNORECASE,
)
# —— 拉人 / affiliate 话术（高精）——
_RECRUIT = re.compile(
    r"(全民代理|全民收单|代理佣金|邀请返利|注册送.*额度|首充.*送|充值送|日赚|月入过万|"
    r"稳赚不赔|一夜暴富|躺赚|被动收入|兼职日结)",
    re.IGNORECASE,
)
# —— Telegram 拉人频道（@xxx / @xxxbot）——
_TG = re.compile(r"@([A-Za-z0-9_]{3,}|[A-Za-z0-9_]{2,}bot)\b")

_SAFETY_GROUPS = {
    "gambling": _GAMBLING,
    "promo_contact": _PROMO_CONTACT,
    "recruit": _RECRUIT,
    "tg_recruit": _TG,
}

# —— 自推 / 推广标记（标题或正文含即硬删）——
_PROMO_TITLE = re.compile(r"^\s*\[推广\]|【推广】|\(推广\)|\[AD\]|\[广告\]", re.IGNORECASE)
_SELF_PROMO = re.compile(
    r"(我(的|们)?(朋友|自己|小)?(弄|做|搞|搭建|开了).{0,8}(小站|中转|平台|站点|API)|"
    r"网友注册送|自家(的)?(API|中转|平台)|个人(的)?(中转|API)|V2EX网友)",
    re.IGNORECASE,
)

# —— emoji 刷屏标题 ——
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F"
    "\U0000FE00-\U0000FE0F\U0000200D]"
)


def is_emoji_spam(text, threshold=0.6):
    """标题去空白后非 emoji 字符占比过低 → 刷屏。"""
    if not text or len(text) <= 1:
        return False
    non_emoji = re.sub(_EMOJI, "", text)
    letters = re.sub(r"\s", "", non_emoji)
    emoji_count = len(text) - len(non_emoji)
    return (emoji_count / len(text)) >= threshold and len(letters) < 3


# —— 未填模板占位符（如 「来源：Dev.to 未闭合）——
_PLACEHOLDER = re.compile(r"「来源：[^」\n]{1,30}(?!\」)")


def safety_hard_filter(text):
    """返回命中的违规类别列表；空列表=安全。无条件优先于 exempt。"""
    if not text:
        return []
    hits = []
    for name, pat in _SAFETY_GROUPS.items():
        if pat.search(text):
            hits.append(name)
    if _PROMO_TITLE.search(text):
        hits.append("promo_tag")
    if _SELF_PROMO.search(text):
        hits.append("self_promo")
    return hits


def is_promo(text):
    return bool(_PROMO_TITLE.search(text) or _SELF_PROMO.search(text))


def has_placeholder(text):
    return bool(_PLACEHOLDER.search(text))
