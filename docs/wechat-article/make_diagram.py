"""生成文章用的两张图：arch.png（分层）与 flow.png（一次 prompt 的链路）。

字体与配色沿用本号前几篇的 make_diagram.py，手机上字号要够大——所以每张图
元素都刻意压到最少，宁可少画几个框，也不让读者去放大。
"""
from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
BG = (247, 247, 252)

BLUE = (56, 132, 222)
PURPLE = (124, 92, 255)
GREEN = (34, 150, 110)
ORANGE = (214, 148, 42)
DARK = (36, 33, 52)
GRAY = (110, 108, 130)

f_box = ImageFont.truetype(FONT, 34)
f_sub = ImageFont.truetype(FONT, 23)
f_small = ImageFont.truetype(FONT, 21)
f_label = ImageFont.truetype(FONT, 22)
f_step = ImageFont.truetype(FONT, 26)


def box(d, x1, y1, x2, y2, fill, outline, width=3, radius=16):
    d.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill,
                        outline=outline, width=width)


def arrow(d, cx, y1, y2, label="", color=(150, 145, 180)):
    d.line([(cx, y1), (cx, y2 - 14)], fill=color, width=4)
    d.polygon([(cx - 12, y2 - 16), (cx + 12, y2 - 16), (cx, y2)], fill=color)
    if label:
        w = d.textlength(label, font=f_label)
        x = cx + 24
        d.rounded_rectangle([x, (y1 + y2) / 2 - 20, x + w + 28, (y1 + y2) / 2 + 20],
                            radius=10, fill=(236, 234, 248))
        d.text((x + 14, (y1 + y2) / 2 - 13), label, font=f_label, fill=(92, 86, 140))


def chip(d, x1, y1, x2, y2, title, desc, tint, line, tcolor):
    box(d, x1, y1, x2, y2, tint, line, 2, 12)
    d.text((x1 + 22, y1 + 16), title, font=f_sub, fill=tcolor)
    d.text((x1 + 22, y1 + 54), desc, font=f_small, fill=GRAY)


# ─────────────────────────────────────────────── arch.png
def make_arch():
    W, H = 1600, 1080
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 1. 客户端
    box(d, 110, 40, 1080, 190, (243, 240, 255), PURPLE, 3)
    d.text((140, 58), "客户端", font=f_box, fill=(72, 48, 170))
    chip(d, 140, 108, 590, 172, "浏览器 SPA", "React + TS · 5k 行 · 深色外观", (255, 255, 255), (222, 214, 250), (72, 48, 170))
    chip(d, 610, 108, 1060, 172, "pi-py CLI", "print / json / rpc 三种模式", (255, 255, 255), (222, 214, 250), (72, 48, 170))

    arrow(d, 595, 192, 262, "WebSocket /api/ws —— 与 --mode rpc 是同一套协议")

    # 2. pi-server
    box(d, 110, 262, 1080, 412, (240, 246, 255), BLUE, 3)
    d.text((140, 280), "pi-server · 3.1k 行", font=f_box, fill=(28, 84, 160))
    d.text((140, 334), "WebSocket · REST · 会话注册表（每用户一个 worker）", font=f_sub, fill=GRAY)
    d.text((140, 370), "认证 · 多租户 · 用量配额 · 审计日志 · 托管单页应用", font=f_sub, fill=GRAY)

    # 右侧：会话文件
    box(d, 1120, 262, 1490, 412, (255, 251, 240), ORANGE, 3)
    d.text((1148, 280), "会话文件", font=f_box, fill=(150, 100, 20))
    d.text((1148, 334), "JSONL v3 · 树形", font=f_sub, fill=GRAY)
    d.text((1148, 370), "与上游 pi 双向互通", font=f_sub, fill=GRAY)

    arrow(d, 595, 414, 484, "核心三层加起来只有 2 个第三方依赖：httpx + pyyaml")

    # 3. 核心框架库
    box(d, 110, 484, 1080, 890, (255, 255, 255), (206, 202, 224), 3)
    d.text((140, 502), "核心框架库 · 可单独作为 SDK 嵌入", font=f_box, fill=DARK)

    rows = [
        (552, "pi-coding-agent · 14.7k 行", "AgentSession 编排 · JSONL v3 会话树 · 7 个内置工具 · 系统提示词 · 扩展协议"),
        (656, "pi-agent-core · 1.8k 行", "agent 循环：steering / follow-up / 工具批 / 中止语义 · Agent 状态机"),
        (760, "pi-ai · 5.6k 行", "provider 适配（Anthropic / OpenAI 兼容）· 12 种流式事件 · 类型与成本计算"),
    ]
    for y, title, desc in rows:
        chip(d, 140, y, 1050, y + 90, title, desc, (249, 248, 255), (216, 210, 240), (92, 66, 190))

    # 右侧：沙盒
    box(d, 1120, 484, 1490, 890, (240, 250, 245), GREEN, 3)
    d.text((1148, 502), "沙盒执行面", font=f_box, fill=(20, 110, 80))
    for i, t in enumerate(["provisioner 服务", "每用户一个容器", "只挂载本工作区",
                           "bash 在容器里跑", "read/write/edit", "仍走宿主（已知取舍）"]):
        d.ellipse([1150, 574 + i * 46 + 10, 1158, 574 + i * 46 + 18], fill=GREEN)
        d.text((1170, 574 + i * 46), t, font=f_small, fill=(70, 68, 92))

    arrow(d, 595, 892, 950, "httpx（流式）")

    # 4. provider
    box(d, 110, 950, 1080, 1056, (245, 245, 250), (200, 198, 216), 3)
    d.text((140, 968), "模型 provider", font=f_box, fill=(70, 68, 92))
    d.text((140, 1016), "Anthropic · OpenAI 兼容端点 · 阿里云百炼", font=f_sub, fill=GRAY)

    img.save("assets/arch.png")
    print("saved assets/arch.png")


# ─────────────────────────────────────────────── flow.png
def make_flow():
    W, H = 1600, 900
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    steps = [
        ("1", "浏览器发 prompt", "WebSocket 一帧 JSON，与 stdio 模式同构", BLUE, (240, 246, 255)),
        ("2", "RpcMode 做 preflight", "配额 / 工具白名单 / 危险命令闸门 —— 这一步就回「已接受」", GREEN, (240, 250, 245)),
        ("3", "AgentSession → agent 循环", "内层循环：没有工具调用才停", PURPLE, (243, 240, 255)),
        ("4", "stream() 拿事件，工具真执行", "tool_execution_end 按完成顺序；toolResult 按源顺序", ORANGE, (255, 251, 240)),
        ("5", "落盘 JSONL v3", "第一条 assistant 消息之前不创建文件", DARK, (245, 245, 250)),
    ]

    y = 50
    for num, title, desc, color, tint in steps:
        box(d, 160, y, 1440, y + 120, tint, color, 3)
        d.ellipse([196, y + 34, 252, y + 90], fill=color)
        tw = d.textlength(num, font=f_step)
        d.text((224 - tw / 2, y + 46), num, font=f_step, fill=(255, 255, 255))
        d.text((286, y + 30), title, font=f_box, fill=color)
        d.text((288, y + 78), desc, font=f_sub, fill=GRAY)
        if num != "5":
            d.line([(800, y + 122), (800, y + 154)], fill=(190, 186, 210), width=4)
            d.polygon([(788, y + 146), (812, y + 146), (800, y + 162)], fill=(190, 186, 210))
        y += 164

    img.save("assets/flow.png")
    print("saved assets/flow.png")




# ──────────────────────────────────────── session-tree.png
def make_tree():
    W, H = 1600, 820
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((110, 30), "分支是原地发生的，只有 fork 才复制文件", font=f_box, fill=DARK)

    # 链条一：同一个文件
    xs = [90, 390, 690, 990, 1290]
    labels = [("header · v3", False), ("user", False), ("assistant", True),
              ("user", False), ("assistant", False)]
    for i, (x, (name, star)) in enumerate(zip(xs, labels)):
        tint = (255, 251, 240) if star else (255, 255, 255)
        line = ORANGE if star else (206, 202, 224)
        box(d, x, 122, x + 260, 230, tint, line, 3)
        w = d.textlength(name, font=f_sub)
        d.text((x + 130 - w / 2, 160), name, font=f_sub, fill=(150, 100, 20) if star else DARK)
        if star:
            d.text((x + 130 - d.textlength("分叉点", font=f_small) / 2, 190), "分叉点",
                   font=f_small, fill=(150, 100, 20))
        if i < len(xs) - 1:
            d.line([(x + 262, 176), (xs[i + 1] - 16, 176)], fill=(190, 186, 210), width=4)
            d.polygon([(xs[i + 1] - 14, 164), (xs[i + 1] - 14, 188), (xs[i + 1], 176)],
                      fill=(190, 186, 210))

    d.text((90, 78), "同一个文件 A.jsonl", font=f_sub, fill=GRAY)

    # 分叉：从 assistant 往下
    d.line([(820, 232), (820, 312)], fill=ORANGE, width=4)
    d.polygon([(808, 306), (832, 306), (820, 322)], fill=ORANGE)

    note = "新 entry 的 parentId 指回分叉点 —— 不新建文件"
    w = d.textlength(note, font=f_label)
    d.rounded_rectangle([560, 332, 560 + w + 32, 372], radius=10, fill=(255, 243, 224))
    d.text((576, 342), note, font=f_label, fill=(150, 100, 20))

    for x, name in [(690, "user"), (990, "assistant")]:
        box(d, x, 402, x + 260, 510, (255, 251, 240), ORANGE, 3)
        w = d.textlength(name, font=f_sub)
        d.text((x + 130 - w / 2, 440), name, font=f_sub, fill=(150, 100, 20))
    d.line([(952, 456), (988, 456)], fill=(190, 186, 210), width=4)
    d.polygon([(974, 444), (974, 468), (990, 456)], fill=(190, 186, 210))

    # fork
    d.line([(250, 232), (250, 632)], fill=GREEN, width=4)
    d.line([(250, 632), (120, 632)], fill=GREEN, width=4)
    d.text((300, 612), "fork(entryId)：把 root → 分叉点 复制进新文件",
           font=f_sub, fill=(20, 110, 80))
    box(d, 120, 650, 1480, 770, (240, 250, 245), GREEN, 3)
    d.text((150, 668), "新文件 B.jsonl", font=f_sub, fill=(20, 110, 80))
    d.text((150, 710), "header · v3  →  user  →  assistant   （此后独立演进，两边都能被 pi 打开）",
           font=f_small, fill=GRAY)

    img.save("assets/session-tree.png")
    print("saved assets/session-tree.png")


if __name__ == "__main__":
    import os
    os.makedirs("assets", exist_ok=True)
    make_arch()
    make_flow()
    make_tree()
