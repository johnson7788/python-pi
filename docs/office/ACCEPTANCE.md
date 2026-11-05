# Office 在线编辑验收清单

## 环境

首次启动会自动构建 web、office 三个编辑器和 xlsx sidecar，然后启动服务：

```bash
./start.sh
# http://127.0.0.1:8787/#/workbench
```

如果需要单独构建：

```bash
./scripts/build-office.sh
./scripts/build-xlsx-sidecar.sh
cd web && npm run typecheck && npm run build
```

## 验收项

| # | 操作 | 预期 |
|---|---|---|
| 1 | 产物面板打开 `.docx` → 在线编辑 | 全屏 DOCX 编辑器，AI 面板隐藏 |
| 2 | 修改正文，点保存，关闭再打开 | 修改保留；顶部显示「已保存」 |
| 3 | 打开另一个标签页编辑同文档 | 第二个显示「正在被另一位用户编辑」 |
| 4 | 点「版本」 | 历史版本列表；恢复后编辑器重载 |
| 5 | 打开 `.xlsx` → 在线编辑 | Univer 表格，无 AI 侧栏，工作表可编辑 |
| 6 | 修改单元格，保存 | 文件写回，重新打开值还在 |
| 7 | 打开 `.pptx` → 在线编辑 | slides 编辑器，缩略图/画布可编辑 |
| 8 | 修改后保存 | 文件写回，父页面收到保存提示 |
| 9 | 并发/锁 | 第二个用户 409，保存被拒绝 |
| 10 | 版本备份 | `<workspace>/.pi/versions/<doc-id>/` 有备份 |

## 最近验证结果

- 后端完整套件：**670 passed**；
- `web` typecheck/build：通过；
- `office` 三套编辑器 build：通过；
- `xlsx-sidecar` cargo release build：通过；
- Playwright 手工链路：
  - DOCX：8 页真实文档，输入 `PARENT-EDIT`，保存后 `word/document.xml` 命中；
  - XLSX：编辑 B7 → `88888`，保存后 `sheet1.xml` 命中；
  - PPTX：打开 5 页文档，`slidesApi.save()` 返回 `ok:true`，父页面显示已保存。

## 当前边界

- 编辑器 ticket 未做，依赖同源 cookie；
- 关闭未保存提示未做；
- XLSX sidecar 是单进程内存会话，暂不支持多副本；
- 复杂 Office 版式保真度需逐份文档验收；
- 编辑器 AI 面板已隐藏，未桥接 python-pi；
- `office-dist` 不提交，部署需先构建。
