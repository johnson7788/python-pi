# 工作台 M11（XLSX 只读预览）

> 2026-11-03。表格产物补齐 XLSX：不引入 openpyxl，用标准库解析 zip/XML，展示工作表网格。

## 已完成

### 后端

- 新增 `GET /api/artifacts/xlsx?path=&workspace=`：
  - 读取 `xl/workbook.xml` 与 `_rels/workbook.xml.rels`，解析工作表名和路径；
  - 读取 `sharedStrings.xml`，支持多段 `<t>`；
  - 解析单元格 `t="s"` / `inlineStr` / 布尔 / 数字；
  - 每表最多 200 行、30 列，最多 8 个工作表；
  - 非 zip / XML 损坏返回 422。
- `.xlsx/.xls` 继续保持 `editable: false`，不会把二进制当文本写回。

### 前端

- 产物面板选中 `.xlsx` 时：
  - 显示工作表 tabs；
  - 只读网格，首行高亮；
  - 切换工作表；
  - 保留下载。
- `.xls`、PDF、PPTX 等仍走只读下载/文本预览。

## 验证

- `npm run typecheck`、`npm run build` 通过；
- 新增 `tests/test_xlsx_preview.py`：sharedStrings、inlineStr、数字、坏文件 422；
- 非 Docker 全量：**636 passed**；
- Playwright：证据表 3 行、切换到检索记录 2 行，无 console 报错。

## 已知边界

- 只读，不能在线编辑 XLSX；
- 忽略样式、合并单元格、列宽、公式（读取缓存值）、日期格式、图表；
- 不支持旧版 `.xls`（二进制格式）；
- 行列上限 200×30、工作表上限 8；
- 空单元格会被压缩到该行已有宽度，不做完整矩形补齐。

## 下一刀

PPT 版式/图片预览，或者技能/插件市场与应用变更审计。
