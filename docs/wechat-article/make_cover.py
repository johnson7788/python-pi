"""生成公众号封面图（1800x766，2.35:1）。

视觉沿用本号前几篇的深蓝底 + 青色竖条，保持同一账号的辨识度。
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1800, 766
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
OUT = "assets/cover.png"

img = Image.new("RGB", (W, H), "#0d1524")
d = ImageDraw.Draw(img)

# 渐变底：深蓝 → 近黑
for y in range(H):
    t = y / H
    top, bot = (18, 32, 56), (8, 12, 22)
    d.line([(0, y), (W, y)], fill=(
        int(top[0] + (bot[0] - top[0]) * t),
        int(top[1] + (bot[1] - top[1]) * t),
        int(top[2] + (bot[2] - top[2]) * t),
    ))

# 右上角青色光晕
glow = Image.new("RGB", (W, H), (0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([W - 760, -460, W + 300, 600], fill=(30, 130, 190))
glow = glow.filter(ImageFilter.GaussianBlur(160))
img = Image.blend(img, glow, 0.32)
d = ImageDraw.Draw(img)

f_title = ImageFont.truetype(FONT, 92)
f_sub = ImageFont.truetype(FONT, 46)
f_tag = ImageFont.truetype(FONT, 30)

# 左侧竖条
d.rounded_rectangle([96, 138, 118, 596], radius=11, fill=(64, 186, 216))

d.text((156, 120), "python-pi", font=f_title, fill=(255, 255, 255))
d.text((160, 258), "开源的多租户", font=f_sub, fill=(186, 226, 240))
d.text((160, 322), "coding agent harness", font=f_sub, fill=(186, 226, 240))
d.text((160, 452), "18 万行 TypeScript → 25.2k 行 Python · 588 个离线测试", font=f_tag, fill=(130, 170, 195))
d.text((160, 500), "会话文件与上游双向互通 · 容器沙盒隔离 · 一条命令部署", font=f_tag, fill=(130, 170, 195))

img.save(OUT)
print("saved", OUT)
