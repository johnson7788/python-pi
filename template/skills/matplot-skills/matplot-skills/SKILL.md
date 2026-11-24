---
name: matplot-skills
description: "文献计量分析的绘图契约（matplotlib）：图型选型、布局与标签避让、中文与字号、面板合成、『图不说谎』检查表。当要画文献分析的图（趋势、国家/机构地图、关键词网络、突现表、时间线、弦图、桑基、树图、雷达）或改造已有图时使用。"
apps: [lit]
---

# 文献分析绘图 · matplotlib 契约

读者拿这些图去写综述、选刊、汇报。他要的是**一眼看懂、数字可信、边界清楚**。

四条硬规矩，违反任一条都算没画完：

1. **数据先行**：图里的每个值都来自 `lit_probe` / `lit_aggregate` / bundle，不许在绘图函数里手写数字；
2. **口径写在图上**：样本量、时间窗、单位、分母——图上写不下就写进图注，图注随图一起交付；
3. **不支持就不画**：覆盖率低于阈值、边太少、样本不足时**跳过该图**，并在正文写一句原因；
4. **图只回答一个问题**。想说两件事就拆两张，或用 A/B/C 面板并写清各自在说什么。

## 一、选型表（按数据形状选，不按喜好选）

| 数据形状 | 图型 | 实现（plugins/lit-analysis/litcharts.py） |
|---|---|---|
| 一个指标随年份 | 柱 + 累计线（双轴） | `fig_trend` |
| 类别排名（期刊/机构/作者/关键词） | 横向条 + 数值直标 | `fig_barh` |
| 连续值分箱 | 直方/柱 | `fig_hist` |
| 构成（4 类以内） | 环形图 | `fig_donut` |
| 两维矩阵（词×年） | 热力图 | `fig_heatmap` |
| 国家/省份 | 地图着色 + 气泡 | `fig_world_map` / `fig_china_map` |
| 双边关系（国家合作） | 弦图 | `fig_chord` |
| 流向（期刊→主题） | 桑基 | `fig_sankey` |
| 共现（词/实体/作者） | 力导向网络 | `fig_network` |
| 共现的“重心” | 核密度热斑 | `fig_network(style="density")` |
| 突现（一段年份显著高于基线） | 突现色条表 | `fig_burst_table` |
| 主题何时起来 | 时间线（年份×簇） | `fig_timeline` |
| 层级（簇→词） | 径向树 | `fig_radial_tree` |
| 两个对象的多个指标 | 雷达 + 分组条 | `fig_radar` / `fig_grouped_bars` |
| 集中度 | Lorenz + Gini | `fig_lorenz` |
| 多图并列对照 | A/B/C 面板 | `compose_panels` |

**不要用**：3D 饼、彩虹配色、双 Y 轴却不标单位、超过 6 类的饼图、没有 n 的比例图。

## 二、画法规范（默认值，改要写理由）

| 项 | 值 | 说明 |
|---|---|---|
| 画布 | 宽 7in，高按内容 `max(2.4, 0.24×条数+1.0)` | 单栏图；面板合成用 1600–1800px 宽 |
| dpi | 200 | 嵌入 Word 约 6.2in 宽时 ≈ 200dpi，打印够 |
| 字号 | 标题 10.5，轴 9，刻度 8，数据标签 7，注 6–6.5 | 全文统一，别一张图一个字号 |
| 轴线 | 去 top/right，网格只留数值轴，虚线 `:` 0.6pt `#d8d8d8` | `_style()` 已封 |
| 颜色 | 主色 `#0f766e`，辅色见 `PALETTE`（8 色） | 同一含义全篇同色；不要为装饰上色 |
| 数值标签 | 条端/柱顶直标，`千分位` | 不要让读者去量坐标轴 |
| 单位 | 轴名带单位（篇/%/人） | 中文图带中文单位 |
| 留白 | `tight_layout()`，`bbox_inches="tight"` | 防标签被裁 |

## 三、中文与字体（最容易翻车的一步）

```python
import matplotlib
matplotlib.use("Agg")                 # 服务端必须；否则要 display
from matplotlib import rcParams
rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS",
                               "WenQuanYi Zen Hei", "Noto Sans CJK SC", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False # 负号变方框就靠这行
rcParams["text.parse_math"] = False    # 标签里出现 $ 会被当公式（疾病分类/机构名很常见）
```

**`parse_math=False` 的副作用**：log 轴的默认刻度会渲染成 `$\mathdefault{10^{4}}$`。
用了对数轴就必须自己设刻度与 formatter：

```python
from matplotlib.ticker import FuncFormatter
ax.set_yticks([1, 10, 100, 1000])
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{int(v):,}"))
```

服务器（`python:3.12-slim`）没有中文字体：镜像装 `fonts-wqy-zenhei`；找不到字体时**降级为英文标签 + 报警**，不要出方框。

## 四、布局算法（“像不像专业工具”的关键）

### 网络图

1. **选边用关联强度，不用原始共现次数**：`strength = w / sqrt(n_a·n_b)`，再取前 N 条；
   否则“非小细胞肺癌”这种枢纽会把整张图画成星形。
2. **社区用模块度**（Newman 贪心，`litcore.communities`，`resolution=1.6`）。
   标签传播在枢纽图上会塌成一色——那是“看起来没分区”，不是“只有一个社区”。
3. **不连通子图必须分开布局再拼网格**，否则斥力把子图推到画布四角、中间全空。
4. **最后统一归一化到 [-0.5, 0.5]**，别让坐标跑到标题上。
5. **边太少就不画**：`min_edges` 默认 10，机构合作这类稀疏网络给 25；低于阈值直接 `return False`。

### 标签避让

- 地图/散点：贪心上下位移 + 引线（`_spread_labels`）；
- 时间线：同年多词按 `dy = 0.16 + 0.15×slot` 错开；
- 径向树：**左半圈不要用径向旋转**（字会倒），改水平排字 + `ha="right"`。

### 密度图

核密度用网格 + 二维高斯的可分离卷积（`outer(iy, ix)`），不要引 scipy；
`contourf(levels=18, cmap="YlGnBu")` + 白色等值线，只标权重前 35% 的词。

## 五、面板合成（A/B/C）

- 同一节的 2–4 张图拼一张，观感立刻接近专业报告；
- `compose_panels(paths, out, labels, cols, width)`：按列宽等比缩放，行高取该行最大，左上角标 A/B/C；
- **列数按长宽比选**：三张横向长图用 3 列；含一张正方形大图（热力/网络）用 2 列，否则高度超过一页；
- 图注要写成 `(A) …；(B) …；(C) …`，和面板标签一一对应。

## 六、常见错误 → 修法

| 现象 | 原因 | 修法 |
|---|---|---|
| 中文变方框 | 无 CJK 字体 | 字体链 + 镜像装字体；找不到就英文降级并告知 |
| 标签里出现 `$…$` | `parse_math` 默认开 | `rcParams["text.parse_math"] = False` |
| log 轴刻度是公式 | 关了 parse_math | 手动 `set_yticks` + `FuncFormatter` |
| 网络图全是星形 | 用原始共现次数选边 | 改关联强度 + 限制枢纽 |
| 网络图只有一种颜色 | 标签传播 / resolution=1 | 改模块度，`resolution=1.6` |
| 子图散在四角 | 多连通分量同场布局 | 分量分开布局 + 网格拼装 |
| 径向标签倒着 | 径向旋转跨过 90° | 左半圈水平排字 |
| 树图里一个色块吃掉 80% | 面积按原始值 | 保留真实面积，但只标前 N；在注里说明首位占比 |
| 图注与图号错位 | 手写图号 | 由 `renumber_figures` 按出现顺序重排 |
| 图超过一页 | 面板 1 列堆叠 | 改 2 列或降宽度 |

## 七、代码模板（最小可用）

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False
rcParams["text.parse_math"] = False

fig, ax = plt.subplots(figsize=(7, 3.2), dpi=200)
ax.barh(labels, values, color="#0f766e", height=0.62)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
ax.grid(axis="x", linestyle=":", linewidth=0.6, color="#d8d8d8")
ax.set_axisbelow(True)
top = max(values) or 1
for i, v in enumerate(values):
    ax.text(v + top * 0.01, i, f"{v:,}", va="center", fontsize=7)
ax.set_xlim(0, top * 1.12)
ax.set_title("载文量 Top20 期刊", fontsize=10.5)
fig.tight_layout()
fig.savefig("out.png", bbox_inches="tight")
plt.close(fig)
```

## 八、交付前检查表

- [ ] 每个数字都能在 bundle/聚合结果里找到；
- [ ] 图上或图注写了 n、时间窗、单位、分母；
- [ ] 覆盖率不足/边太少的图**没有出现**，正文给了原因；
- [ ] 中文不是方框，负号不是方框，标签里没有意外公式；
- [ ] 网络图有 ≥3 种社区颜色，且不是星形；
- [ ] 面板标签（A/B/C）与图注一一对应；
- [ ] 图号连续、与正文引用一致（由 `renumber_figures` 保证）；
- [ ] 单张图在 Word 里不超过一页。
