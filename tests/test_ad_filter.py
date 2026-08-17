import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
import ad_filter as adf

# 线上真实博彩样本（dajiayouxuan.com/13469.html 泄漏内容）
CASINO = (
    "😀😀😀😀😀😀😀😀😀😀😀😀😀\n"
    "🤑 铂莱娱乐 8年老品牌 广告费每月投入高达500万U，全网担保上押1300万U，"
    "送“野鸡黑台”一句话，勿伤玩家利益，勿毁博彩信誉。\n"
    "🫢 【贵宾会 750.cc 】 强势启航，集团背书，信誉担保，全网首发，引爆彩票娱乐新纪元！"
    "六合彩49倍 挑战全网心水 全民代理全民收单 助您白手起家 加拿大28全网最高赔率。\n"
    "😎 PG电子爆出200万u巨奖。\n"
    "😀😀😀😀: @BLKF0  😀😀😀😀: @bolaidaili  🟢🟢😀😄: @bolaiylc_bot  bolai178.cc"
)

PROMO = "[推广] 和朋友弄了个 AI API 中转小站，V2EX 网友注册送 20 元额度"


def test_casino_blocked():
    assert "gambling" in adf.safety_hard_filter(CASINO)
    assert "tg_recruit" in adf.safety_hard_filter(CASINO)


def test_promo_blocked():
    assert adf.is_promo(PROMO)


def test_emoji_spam():
    assert adf.is_emoji_spam("😀😀😀😀😀😀😀😀😀😀😀😀😀")


def test_placeholder():
    assert adf.has_placeholder("「来源：Dev.to")


def test_legit_not_blocked():
    legit = "用 Notion + 飞书多维表格搭建一个客户跟进系统，启动成本 0，适合自由职业者获客。"
    assert adf.safety_hard_filter(legit) == []
    assert not adf.is_promo(legit)
    assert not adf.is_emoji_spam("用 Notion 搭客户系统")
