---
name: chart-gallery
description: "ECharts 6 图表选型与模板索引：37 种图（柱/条/堆叠/瀑布/折线/面积/区间带/主题河流/饼环/玫瑰/双层环/矩形树/旭日/桑基/和弦/直方/箱线/蜂群/热力/日历/平行坐标/散点/气泡/双轴/关系图/矩阵/仪表盘/极坐标/K线）的适用与反模式，option 由 chart_templates 工具给出（都经过渲染测试）。当要选图型、或想确认某种图该怎么画时使用。"
---

# 图表选型与模板

**不要凭记忆写 option。** 37 个模板都在 `chart_templates` 工具里，每个都渲染过：

```
chart_templates                                        # 列出全部（按族）
chart_templates id="waterfall"                         # 取完整 spec 骨架
chart_templates id="bar-basic" path="data/sales.csv"   # 顺便校验列名
```

取到的是**完整 spec**，你只需要改 `bind.path` 和 `title`，然后 `chart_check` → `chart_render_html`。

## 按问题选图，不按图名选图

| 你要回答 | 用它 | 不要用 |
|---|---|---|
| 谁大谁小 | `bar-basic`、`bar-horizontal`（类目多/名字长） | 饼图 |
| 两组比一比 | `bar-grouped` | 双轴 |
| 构成是什么 | `bar-stacked`、`bar-percent`、`treemap` | 饼图（>6 类） |
| 从 A 到 B 怎么变的 | `waterfall` | 折线（没有累计语义） |
| 目标达成没有 | `bar-bullet`、`gauge` | 双轴 |
| 趋势往哪走 | `line-basic`、`line-area`、`line-step` | 柱状（类目多时） |
| 多指标一起看 | `line-multi`、`combo-dual`（量纲不同，须说明） | 一张图塞 5 条线 |
| 构成随时间变 | `area-stacked`、`bar-percent`、`theme-river` | 多个饼图并排 |
| 不确定有多大 | `area-band`、`boxplot` | 只给均值 |
| 分布长什么样 | `histogram`、`scatter-jitter`、`boxplot` | 一根均值柱 |
| 两个变量有没有关系 | `scatter`、`bubble`、`heatmap` | 双轴折线 |
| 谁和谁有关 | `sankey`（流向）、`chord`（双向）、`graph`（网络） | 饼图 |
| 占比 + 层级 | `treemap`、`sunburst`、`pie-nested` | 三层以上的饼 |
| 时间 × 类目的密度 | `heatmap`、`calendar-heatmap` | 折线打结 |
| 多维综合打分 | `parallel`、`matrix` | 雷达（>3 个对象） |
| 价格区间 | `candlestick` | 折线（丢掉高低点） |
| 类目多又想省空间 | `polar-bar`、`bar-horizontal` | 玫瑰图（读不准） |

## 六个族的一句话

- **比较（compare）**：比的是"同一把尺子下的高低"，所以轴必须从 0 起、类目要排序。
- **趋势（trend）**：比的是"方向和速度"，点数多时去掉点、不要默认平滑。
- **构成（composition）**：比的是"部分与整体"，占比先算好；结构性数据（桑基/和弦/旭日/矩形树）
  的节点关系可以直接写，但**数值仍要来自 profile 的汇总**。
- **分布（distribution）**：比的是"形状和离散"，必须给 n 和分箱/分组规则。
- **关系（relation）**：比的是"一起变"，相关不等于因果；双轴是最后手段。
- **专用（special）**：仪表盘只放一个指标；K 线的颜色约定要跟受众一致。

## 取到模板后只改这几处

1. `bind.path` 改成真实数据文件（工具里的 `<把这里改成你的数据文件>` 必须替换）；
2. `bind.columns` 保持模板要求的列名，除非你同时改了 `encode`；
3. `page.title` / `charts[].title` 写成**结论**，不是字段名；
4. `charts[].note` 写"怎么读 / 哪里不能比"；
5. `notes.unit` / `source` / `caveat` 必须填——页脚会原样显示。

## 编码约定

- 一律 `dataset: {}` + `series.encode`：数据由工具绑定，**不要**写 `dataset.source`；
- 直角坐标用 `encode: {x, y}`；极坐标柱用 `encode: {angle, radius}`（写成 x/y 会渲染成空白）；
- 箱线 `encode.y` 是五数 `[min, q1, median, q3, max]`；K 线是 `[open, close, low, high]`（顺序错图乱但不报错）；
- 饼/环/玫瑰用 `encode: {itemName, value}`；矩形树/旭日/桑基/和弦/关系图的节点与连线直接写在 `option` 里。

## 反模式（命中就换）

- 饼图 >6 类、3D 饼、环形套环形超过两层；
- 柱状图 Y 轴不从 0 起；断轴没标注；
- 双轴没写两个单位；两条线交叉点被当成"发生了转折"；
- 用平均值代替分布；用 0 填补缺失再画趋势；
- 雷达图放 4 个以上对象；热力图色阶用红绿；
- 一张图里同时有"总量"和"占比"两种口径还不说明。

## 不可用（当前环境）

- **3D / GL**（`bar3D`/`globe`/`surface`/`scatterGL`…）：需要 `echarts-gl`，内置包只有核心；
- **custom 系列**：`renderItem` 是函数，JSON 存不了；
- **地图**：`registerMap` 要在 JS 里注册 GeoJSON，中国地图另有审图号合规问题。

这三种情况**换二维表达**，并在对话里说清为什么换。
