# deck.json 字段说明

一份 deck 是一个 JSON 对象。顶层控制整份的元信息与主题，`pages` 是一页一个对象。

## 顶层

| 字段 | 类型 | 必填 | 作用 |
|---|---|---|---|
| `title` | string | 建议 | 整份标题；封面未写 `title` 时兜底 |
| `subtitle` | string | 否 | 封面副标题兜底 |
| `brand` | string | 否 | 封面右上角/结尾的署名 |
| `date` | string | 否 | 封面与结尾的时间 |
| `label` | string | 否 | 页脚左侧的小字（默认取 `title`） |
| `theme` | object | 否 | 见下 |
| `created` | string | 否 | 文件元数据时间，默认固定值，保证可复现 |
| `pages` | array | **是** | 每页一个对象，顺序即放映顺序 |

## theme

```json
{ "preset": "midnight", "fonts": { "latin": "Arial", "ea": "Microsoft YaHei" } }
```

主题同时决定深色版底色、主色、浅主色、分割线和正文色，换 preset 是整套换。

| preset | 气质 | 适合 |
|---|---|---|
| `midnight` | 深海军蓝 + 冷灰 | 商务汇报、评审、路演（默认） |
| `forest` | 墨绿 | 医疗、可持续、研究 |
| `terracotta` | 砖红 | 品牌、内容、文化 |
| `teal` | 墨青 | 科技、数据、产品 |
| `charcoal` | 石墨 | 克制、内部复盘 |

- `accent`：覆盖主色，6 位十六进制，不要带 `#` 以外的字符；
- `fonts.ea`：中文字体。Windows/Office 用 `Microsoft YaHei`，macOS 可用 `PingFang SC`；
- 同一份 deck 只用一套主题，不要逐页换色。

## 页对象通用字段

| 字段 | 作用 |
|---|---|
| `layout` | **必填**，见 [`layouts.md`](layouts.md) |
| `title` | 页面标题（多数版式的必填项） |
| `kicker` | 标题上方的小标签，2–6 字，全大写英文时自动转大写 |
| `note` | 页面底部的口径/来源说明，一行以内 |

## 各版式的专属字段

### cover

```json
{ "layout": "cover", "eyebrow": "内部评审", "title": "主题", "subtitle": "一句话定位", "meta": "2026 年 11 月\n团队" }
```

`image` 给一张 PNG/JPEG 时，封面变成整版图片 + 深色蒙版，文字压在图上；不给就是整版深色。
`meta` 支持换行（`\n`），常见是「日期 + 团队」两行。

### section

```json
{ "layout": "section", "number": "01", "title": "章节标题", "subtitle": "一句导语" }
```

`number` 不写时用页码自动补。

### agenda

```json
{ "layout": "agenda", "title": "今天讲三件事", "items": ["问题", "主线一", "主线二", "节奏"] }
```

`items` 为字符串数组；超过 5 条自动分两栏，最多 8 条。
写成 `标题：副题` 时，标题加粗、副题变灰（如 `问题：能力很多，交付很少`）。

### bullets

```json
{ "layout": "bullets", "kicker": "现状", "title": "四类交付断点", "bullets": ["**停在对话里**：…", "参数靠猜：…"], "note": "…" }
```

`bullets` 每条一行：冒号前是加粗标题，冒号后是灰色说明，行间只有一条细线。
可选 `lede` 在标题下写一句引导语（一句话以内，不要写成第二段正文）。
建议 3–5 条、每条 ≤50 字；没有冒号的句子会整句当标题，长句会显得笨重。

### two_col

```json
{
  "layout": "two_col",
  "title": "两条路",
  "left": { "heading": "让模型写文件", "bullets": ["…", "…"] },
  "right": { "heading": "模型写结构", "bullets": ["…", "…"] }
}
```

两侧各 3–4 条；也可以用 `paragraphs`（字符串数组）写整段解释，和 `bullets` 二选一。
卡片高度按内容自适应，内容少时两张卡会整体居中。

### stats

```json
{ "layout": "stats", "kicker": "效率", "title": "交付效率的分水岭",
  "stats": [
    { "value": "3×", "label": "平均返工次数下降" },
    { "value": "85%", "label": "首次即可交付" }
  ],
  "caption": "一句结论", "note": "口径与来源" }
```

2–4 组大数字并排，76pt，数字下面一行小标签；`value` 建议 ≤5 个字符，长值会降字号。`caption` 是下面那句结论，
不要写成复述；`note` 只放口径、样本量、时间范围。

### image_text

```json
{ "layout": "image_text", "image": "assets/photo.jpg", "kicker": "方法",
  "title": "一次生成，两次使用",
  "paragraphs": ["第一段", "第二段"],
  "note": "图注或口径" }
```

左半版是图片（按框架裁切，不拉伸），右半版是标题 + 2–4 段短文。`image` 是相对 spec 文件的路径，
支持 PNG / JPEG。没有合适配图时不要硬用这个版式，用 `bullets` 或 `stats`。

### big_number

```json
{ "layout": "big_number", "title": "交付效率", "number": "3×", "caption": "返工次数下降", "bullets": ["…", "…"] }
```

`number` 建议 ≤7 个字符（`38%`、`3×`、`1.2 亿`）；超过 7 字符字号会自动降一档。
也可以传 `stats` 数组，此时渲染成和 `stats` 版式一样的卡片行。

### quote

```json
{ "layout": "quote", "text": "一句值得停下来的话。", "source": "— 谁说的" }
```

`title` 可以作为 `text` 的兜底；不要同时写两段长文。

### timeline

```json
{ "layout": "timeline", "title": "三个里程碑", "items": [
  { "when": "M1 · 2 周", "what": "先跑通渲染" },
  { "when": "M2 · 1 个月", "what": "加风格门" }
] }
```

最多 5 个节点，`what` 控制在 20 字内。

### table

```json
{ "layout": "table", "title": "验收口径", "columns": ["维度", "当前", "目标"], "rows": [["首次可交付率", "40%", "≥85%"]] }
```

≤5 列、≤6 行；单元格支持 `**加粗**`；不要用表格排版长段落。

### closing

```json
{ "layout": "closing", "title": "谢谢", "subtitle": "下一步：…", "meta": "联系方式 / 时间" }
```

## 检查清单（写完后自己过一遍）

- [ ] 第一页 `cover`，最后一页 `closing`；
- [ ] 每页只有一个主张，标题是结论不是主题词；
- [ ] 没有连续 3 页同一版式；
- [ ] 至少有一页 `stats` 或 `table`，不是全篇 bullet；
- [ ] 每条 bullet 都能一句话读完；
- [ ] 所有数字有来源，或标了「约」；
- [ ] `check` 没有 error。
