# 版式速查

12 种版式，全部 16:9。白色画布，靠留白、整版色块和大字号分区。

| 版式 | 必填 | 可选 | 容量 | 什么时候用 |
|---|---|---|---|---|
| `cover` | `title` | `subtitle` `eyebrow` `meta` `image` | 标题 ≤2 行 | 第 1 页；深色或整版图片封面 |
| `section` | `title` | `number` `subtitle` | 标题 1 行 | 章节切换；整版主色 + 超大编号 |
| `agenda` | `items` | `title` | 3–8 条 | 目录；大编号 + 粗标题 + 灰副题 |
| `bullets` | `bullets` | `title` `kicker` `lede` `note` | 3–5 条 | 并列观点；大编号 + 判断/证据两行 |
| `two_col` | `title` | `left` `right` `note` | 每侧 3–4 条 | 前后对比、方案 A/B；中间一条细线 |
| `stats` | `stats` | `title` `kicker` `caption` `note` | 2–4 组 | 一组关键指标，76pt 大数字 |
| `big_number` | `number` | `title` `caption` `bullets` | 1 个数字 | 一个数字就是结论（也可传 `stats`） |
| `quote` | `text` | `source` | 1 句，≤3 行 | 金句；整版深色、44pt |
| `timeline` | `items` | `title` `kicker` | 3–5 节点 | 路线图；横线 + 圆点 + 文字 |
| `table` | `columns` | `rows` `title` `note` | ≤5 列 ≤6 行 | 验收口径、多维对照；只有横线 |
| `image_text` | `title` `image` | `kicker` `paragraphs` `note` | 半版图 + 3–4 段 | 有配图时的首选 |
| `closing` | `title` | `subtitle` `meta` | 1 行标题 | 最后一页；与封面同构 |

## 节奏建议

一份 10 页左右的汇报，常见骨架：

```
cover → agenda → section → bullets → two_col → stats
      → image_text → timeline → table → quote → closing
```

- 每 4–6 页一个 `section`，让听众知道走到哪了；
- **深-浅-深**：封面、章节、金句、结尾用深色或主色整版，内容页留白；
- 连续 `bullets` 最多两页；第三页换成 `two_col` 或 `stats`；
- 全篇至少一页 `stats` / `table` / `image_text`：纯文字的 deck 永远像会议纪要；
- 不要每页都套同一种网格；版式轮换本身就是节奏。

## 字号与几何（脚本内部规则）

- 画布 13.333 × 7.5 in；左右边距 0.9 in；内容底部 6.75 in；
- 封面标题 60pt；章节标题 46pt；页标题 46pt；大数字 76pt（单数字页 150pt）；
- 正文 15–16pt；注释 11pt；页脚 9pt；
- 封面/结尾/金句：整版深色；章节：整版主色；内容页：白底。

内容超出时脚本不会截断文字，而是缩小字号；缩到下限仍超出就说明**这一页放不下**：拆页或删字。
