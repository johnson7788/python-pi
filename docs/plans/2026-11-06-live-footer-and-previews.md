# 深度求索状态行与 HTML/Markdown 预览

## 运行状态

运行中的回合不再在顶部显示「处理中」，改为在过程行末尾显示：

```
深度求索中，用时 2秒 ···
```

- 小图标呼吸闪烁；
- 20 秒无输出：`等待输出，用时 …`；
- 45 秒无输出：`可能卡住，用时 …`；
- 完成后该行消失，换成顶部的 `已完成，用时 …` 折叠控制。

![深度求索状态](../workbench-live-footer.png)

## HTML / Markdown 产物

### Markdown

- 打开 `.md/.mdx` 默认进入渲染预览，不再是源码 textarea；
- 工具栏可以切回「编辑」；
- 表格、列表、代码、标题走现有 `renderMarkdown`。

### HTML

- 打开 `.html/.htm` 默认在右侧渲染；
- 使用 sandbox iframe（禁止脚本），只展示静态 HTML/CSS；
- 工具栏「编辑」切到源码，保存仍写回 workspace；
- 预览填满右侧产物区。

![HTML 预览](../workbench-html-preview.png)

## 验证

- Markdown：`.wb-artifact-markdown` = 1，默认无 textarea；
- HTML：`.wb-html-preview` = 1，默认无 textarea；
- 运行态：`.wb-turn-live` 显示 `深度求索中，用时 …`，顶部无「处理中」头；
- `web` typecheck/build 通过。

## 边界

- HTML 预览禁用脚本和表单，适合生成页/报告；需要交互的 HTML 需要单独开放 sandbox；
- 外部相对资源（图片/CSS）暂不保证加载，生成时最好内联；
- Markdown 预览只是静态渲染，不支持内嵌 HTML 交互。
