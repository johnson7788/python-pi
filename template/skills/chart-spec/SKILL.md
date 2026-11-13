---
name: chart-spec
description: "数据绘图的交付契约：先看数据结构再选图型，spec + 工具生成 HTML，数字必须由工具从数据文件绑定。含图表选型决策树、视觉规范、『图表不说谎』检查表和交付话术。当用户要『画图/做个图/把数据可视化/做一页看板/这张图改一下』时使用。"
---

# 数据绘图 · 交付契约

读者拿这张图去开会、写周报、做汇报。他要的是**一眼看懂、数字可信、边界清楚**。

三条硬规矩，违反任何一条都算没做完：

1. **图里的每个数字都来自数据文件**，由 `chart_render_html` 从 `bind` 绑定，页脚带路径、行数和 sha256；
2. **HTML 只能由工具生成**（`chart_render_html`），不许手写 HTML/JS——产物面板给 iframe 开了脚本，
   手写等于让模型往工作台里塞任意代码；
3. **图回答一个问题**。一张图想同时说三件事，就拆成三张。

## 一、顺序固定

```
chart_data_profile  →  chart_templates  →  改 spec  →  chart_check  →  chart_render_html  →  present
```

- **先 profile**：列名、类型、缺失、基数、数值范围。不要凭表名猜字段，也不要把整张表读进上下文。
- **再取模板**：`chart_templates id="<id>"` 给出完整 spec（37 种图，都渲染过）。只改 `bind.path`、
  标题和文案；**不要凭记忆写 option**——漏一个 `dataset: {}` 或 encode 写错列，图就是空白。
- **spec 存成** `charts/<名字>.spec.json`；HTML 默认同名 `charts/<名字>.html`。
- `chart_check` 报 error 必须改；只有 warning 可以渲染，但每条都要在 `note` 或 `notes.notes` 里交代。
- 交付时 `present` 两个文件：HTML（给人看）和 spec（给下一次改）。

## 二、spec 的形状

```json
{
  "schema": "pi.chart/1",
  "page": { "title": "2025 各月销售额", "subtitle": "口径：含税，华东/华北合并", "columns": 1, "theme": "auto" },
  "bind": { "path": "data/sales.csv", "sheet": null, "columns": ["month", "sales", "margin"] },
  "charts": [
    {
      "title": "3 月起增速回落，但未转负",
      "option": {
        "dataset": {},
        "grid": { "left": 64, "right": 28, "top": 40, "bottom": 56 },
        "tooltip": { "trigger": "axis" },
        "xAxis": { "type": "category", "name": "月份", "axisLabel": { "hideOverlap": true } },
        "yAxis": { "type": "value", "name": "销售额（万元）" },
        "series": [{ "type": "line", "encode": { "x": "month", "y": "sales" }, "smooth": true }]
      },
      "note": "2 月含春节，环比不可比"
    }
  ],
  "notes": { "unit": "万元", "source": "财务月报 2025", "caveat": "12 月为预估值" }
}
```

规则：

- `dataset` 留成 `{}`。**写了 `dataset.source` 会直接报错**——数据由工具填；
- `series` 用 `encode` 指定列，列名必须出现在 `bind.columns`；
- 手写 `series.data` 的数字会被警告：那是"看起来对"的最大来源；
- `aggregate` 只在确实需要"按类目汇总"时用（`{"by": "month", "op": "sum"}`）；它会写在警告里，图注要跟进；
- `bind.limit` 是**明确截断**，不要拿它掩盖"数据太多"；超 5000 行请聚合或拆页。

## 三、选图型（按数据结构，不按个人偏好）

| 数据形状 | 用它（chart_templates id） | 不要用它 |
|---|---|---|
| 时间 × 数值，看趋势 | `line-basic`、`line-area` | 饼图、3D 柱 |
| 时间 × 多指标，看构成变化 | `area-stacked`、`bar-percent` | 多条量纲不同的折线挤一张图 |
| 分类 ≤12 × 数值，比大小 | `bar-basic`；类目多/名字长 → `bar-horizontal` | 饼图（分类 >6）、雷达 |
| 整体占比，≤6 类 | `pie-basic`、`doughnut`；仍要给出绝对值 | 环形套环形 |
| 两个数值，看相关 | `scatter`、`bubble` | 双轴折线 |
| 分布 | `histogram`、`boxplot`、`scatter-jitter` | 平均值一根柱 |
| 增减拆解 | `waterfall` | 折线（没有累计语义） |
| 目标达成 | `bar-bullet`、`gauge` | 双轴 |
| 流程/层级 | `sankey`、`treemap`、`sunburst`、`chord` | 3D |
| 多维综合 | `parallel`、`matrix` | 雷达放 4 个以上对象 |

完整 37 种见 `chart-gallery` 技能；option 一律用 `chart_templates` 取，不要手写。

**默认不做**：3D、GL、地图、custom 系列。3D/GL 需要 `echarts-gl`（内置包没有，画出来是空白）；
custom 的 `renderItem` 是函数，JSON 存不了；地图要自备 GeoJSON，中国地图还涉及审图号。
用户明确要时，先说清代价，再给二维替代方案。

## 四、视觉规范

- 字号：标题 13px（面板内）、轴标签 11–12px；不要为了"塞下"把字缩到 10px 以下；
- 网格：`grid: {left: 64, right: 28, top: 40, bottom: 56}`，按轴名长度微调；
- 单位写进轴名（`yAxis.name: "销售额（万元）"`），不要在 `notes.unit` 之外再省略；
- 千分位用 `axisLabel.formatter` 的字符串模板或 `valueFormatter` 字符串；
- 颜色克制：同一页 ≤5 种主色，顺序色用单色渐变；不要红绿对立（色盲）；
- 柱状图保留 0 基线；折线图**不要**默认截断 Y 轴；
- 类目名长就横向条形，不要斜着写 45° 标签；
- 每张图 `note` 写一句"怎么读/哪里不能比"，没有就留空，不要写"如图所示"；
- 标记线与标记点：`markLine.label.position` 用 `insideEndTop`（默认在右边缘会被裁），
  `markPoint` 只标 max/min 两个，标多了会盖住轴线和数据点。

## 五、图表不说谎（渲染前的自检）

逐条过，命中就改或在图注里说明：

| 症状 | 后果 | 处理 |
|---|---|---|
| Y 轴不从 0 开始（柱状） | 差异被放大数倍 | 回到 0；确实要放大就用折线并标注 |
| 断轴（`axis breaks`） | 视觉距离失真 | 图注写明断点与倍数 |
| 双 Y 轴 | 两条线的高低是人为的 | 说明各自单位与为什么必须同图；能拆就拆 |
| 饼图 >6 类 | 小扇区读不出 | 换条形，或合并"其他" |
| 百分比堆叠混正负 | 视觉上互相抵消 | 拆成正/负两组，或改成分组柱 |
| 3D / 透视 | 近大远小，读数不可能准 | 换二维 |
| 只给均值 | 掩盖分布与异常 | 加分布、误差线或 n |
| 抽样/截断未标 | 读者以为全量 | `bind.limit` 的警告必须进图注 |
| 相关写成因果 | 结论越界 | 正文明确"相关不等于因果" |

## 六、交付话术

对话里按这个顺序，短：

1. **一句话结论**（带数字）；
2. **图在说什么**：每张图一行，写"它回答的问题"，不是复述标题；
3. **边界**：口径、缺失、样本量、不能下的结论；
4. **文件**：`present` 的 HTML 与 spec（HTML 可交互，PNG/CSV 按钮在页面里）。

不要贴一大段 option JSON 到对话里；spec 是文件。

## 七、失败处理

- `chart_data_profile` 报不支持的格式：`.xls` 请用户另存 `.xlsx`/`.csv`；
- `chart_check` 报 `custom`/`3D`/`GL`：换成内置类型，或明确告诉用户"这个图型当前不支持"；
- HTML 超过 480KB：先 `aggregate` 聚合成类目级，再减列；不要靠 `limit` 硬截；
- 数据里百分比带 `%`：绑定后是数字，轴/标签要补 `%`（profile 会提示）；
- 缺失值：ECharts 用 `null` 断开，不要用 0 填补——0 是一条假数据。
