# 工作台 M10（产物结构化查看/编辑）

> 2026-11-03。产物面板从“纯文本预览”升级：CSV/TSV 表格网格编辑、Markdown 预览、PPTX 幻灯片文本只读预览。

## 已完成

### 后端

- 产物可编辑白名单收紧为文本类型：`md/mdx/txt/html/htm/csv/tsv/json/yaml/yml`；
  - `.xlsx/.xls/.pptx/.pdf` 不再标记为可编辑，避免把二进制当文本保存。
- 新增 `GET /api/artifacts/pptx?path=&workspace=`：
  - 用标准库 `zipfile + ElementTree` 读取 OOXML；
  - 按 `slideN.xml` 排序，提取 `a:t` 文本；
  - 返回 `{slides:[{number,texts}]}`，不依赖 python-pptx；
  - 非 zip / XML 损坏返回 422。
- 产物列表仍然只列工作区文件，复用现有 workspace 隔离。

### 前端

- CSV/TSV：
  - 默认表格网格，表头行高亮，单元格直接编辑；
  - “表格/源码”切换；
  - “+ 行 / + 列 / − 行”；
  - 保存时序列化为 CSV/TSV（支持引号转义）。
- Markdown：编辑/预览切换，预览复用现有 `renderMarkdown`。
- PPTX：右侧按页展示提取到的文本；只读，保留下载；不再显示“可编辑”假象。
- 其他二进制：只读下载。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- 新增 `tests/test_pptx_preview.py`：坏 XML 422、正常 pptx 提取文本；
- 非 Docker 全量：**635 passed**；
- Playwright：CSV 表格 3 行、改单元格出现未保存提示、PPTX 3 页文本、Markdown 预览，无 console 报错。

## 已知边界

- CSV 解析支持引号转义，但不是完整 RFC 4180 实现，也没有列宽/排序/公式；
- PPTX 只提取文本，没有版式、图片、图表，也不能在线编辑；
- XLSX 仍只读下载，没有结构化解析；
- 表格没有虚拟滚动，超大 CSV 会卡；
- 保存没有冲突检测和版本历史。

## 下一刀

XLSX 只读解析、PPT 版式预览，或者技能/插件市场与应用变更审计。
