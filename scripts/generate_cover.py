#!/usr/bin/env python3
"""生成公众号封面图：『副业日报 + 日期』，与文章品牌色一致。

设计：深绿渐变背景（品牌绿 #2f6b5e → 深绿 #1c4a40）+ 半透明奶白大圆点缀 +
琥珀色细线 + 大标题『副业日报』+ 琥珀色日期 + 底部栏目副标题。
全部用 PIL 绘制几何形状，不使用任何图片素材，规避版权风险。

字体：优先用免费授权字体（Noto Sans CJK / SIL OFL，CNB 流水线已 apt 安装
fonts-noto-cjk）；本地回退到 macOS 内置 STHeiti（预览用）；再不行降级告警。
绝不依赖任何商业/收费字体。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ── 文章品牌色（与 publish_wp.py 保持一致）──
BRAND = (47, 107, 94)        # #2f6b5e 品牌绿
BRAND_DARK = (28, 74, 64)    # #1c4a40 深绿
CREAM = (250, 249, 247)      # #faf9f7 奶白
AMBER = (232, 176, 75)       # #e8b04b 琥珀点缀
WHITE = (255, 255, 255)

# 字体候选（粗体优先，依次回退；索引用于 .ttc 集合）
FONT_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    os.environ.get("FONT_BOLD", ""),
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]
FONT_REG = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    os.environ.get("FONT_REGULAR", ""),
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:/Windows/Fonts/msyh.ttc",
]


def _load(fonts, size):
    for p in fonts:
        if not p:
            continue
        for idx in (0, 1, 2):
            try:
                return ImageFont.truetype(p, size, index=idx)
            except Exception:
                continue
    return ImageFont.load_default()


def make_cover(date_str, out_path):
    """生成封面 PNG。date_str 形如 2026-08-15。"""
    W, H = 900, 500
    img = Image.new("RGB", (W, H))
    # 竖向渐变（品牌绿 → 深绿）
    for y in range(H):
        t = y / (H - 1)
        r = int(BRAND[0] + (BRAND_DARK[0] - BRAND[0]) * t)
        g = int(BRAND[1] + (BRAND_DARK[1] - BRAND[1]) * t)
        b = int(BRAND[2] + (BRAND_DARK[2] - BRAND[2]) * t)
        ImageDraw.Draw(img).line([(0, y), (W, y)], fill=(r, g, b))

    d = ImageDraw.Draw(img, "RGBA")
    # 右下半透明奶白大圆（点缀，部分出画）
    d.ellipse([W - 360, H - 360, W + 280, H + 280], fill=(*CREAM, 24))
    # 左上琥珀小圆点缀
    d.ellipse([-130, -130, 150, 150], fill=(*AMBER, 28))

    pad = 74
    date_disp = date_str.replace("-", ".")

    # 顶部 kicker
    kf = _load(FONT_REG, 25)
    d.text((pad, 104), "AI 副业日报 · 每日自动生成", font=kf, fill=(*CREAM, 205))

    # 主标题『副业日报』
    tf = _load(FONT_BOLD, 132)
    d.text((pad, 150), "副业日报", font=tf, fill=WHITE)

    # 标题下方的琥珀细线
    d.line([(pad, 304), (pad + 300, 304)], fill=(*AMBER, 200), width=3)

    # 日期（琥珀色）
    df = _load(FONT_BOLD, 54)
    d.text((pad, 322), date_disp, font=df, fill=AMBER)

    # 底部栏目副标题
    sf = _load(FONT_REG, 27)
    d.text((pad, H - 70), "项目机会 · 增长运营 · 观点心法", font=sf, fill=(*CREAM, 175))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    return out_path


if __name__ == "__main__":
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/cover_preview.png"
    ds = sys.argv[1] if len(sys.argv) > 1 else "2026-08-15"
    print("cover ->", make_cover(ds, out))
