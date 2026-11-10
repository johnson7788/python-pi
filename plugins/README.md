# 官方插件包（starter pack）

这批插件是「让团队第一天就能用起来」的四个起点。共同特征：**只读、边界清晰、可复核**。
代码就是文档——每个目录都是独立的 `plugin.json` + `main.py`，可以直接改。

| 插件 | 工具 | 依赖 | 适合谁 |
|---|---|---|---|
| [`clinical-calc`](clinical-calc) | `body_metrics`、`renal_function`、`risk_scores`、`liver_scores` | 无 | 医学、临床、药企 |
| [`pubmed`](pubmed) | `pubmed_search`、`pubmed_abstract` | 联网（NCBI） | 医学、研发、医学事务 |
| [`web-fetch`](web-fetch) | `web_fetch` | 联网 | 所有人 |
| [`csv-tools`](csv-tools) | `csv_profile`、`csv_query` | 无 | 数据、运营、分析 |

## 安装

两个入口，选一个：

**市场（推荐）**：插件页 → 市场 → 找到条目 → 安装 → 在目标应用上启用。

**zip**：管理员在插件页上传。打包：

```bash
./plugins/build.sh            # 产物在 dist/
```

`build.sh` 会先跑 `pikit check`，任何一个插件有 error 都不会打包。

## 为什么是这四个

- **临床计算器**：模型算数不可靠、公式版本记不准（CKD-EPI 2021 去种族项、MELD-Na 的上下限）。
  把确定性知识交给代码，输出里带公式与出处，医生能复核。零依赖、零风险。
- **PubMed**：团队每天都在查文献。插件固定检索式构造、限流、字段裁剪，不会把整页 XML
  塞进上下文，也不会因为没带 `tool`/`email` 被 NCBI 限流。
- **网页抓取**：模型读网页目前只能 `curl` 一坨 HTML。`web_fetch` 把抓取、去噪、裁剪、
  SSRF 防护做成一步；同时是「怎么在插件里安全地联网」的参考实现。
- **CSV 工具**：业务数据大量以表格流转。先 `csv_profile` 看结构，再 `csv_query` 挑记录，
  比让模型读整份 CSV 省一个数量级 token。

## 开发约定

这四个插件同时是模板：

- `plugin.json` 用 v2 字段（`config` / `limits` / `permissions` / `requires`），旧宿主会忽略未知字段；
- 工具参数错误、下游失败一律 `return` 文本，不 `raise`；
- 文件访问只用 `workspace_path(api, "相对路径")`，不用 `os.getcwd()`；
- 联网用 `asyncio.to_thread` 包住阻塞 IO，带超时与响应大小上限；
- 每个插件带 README，写清「装、用、边界」。

改完跑：

```bash
python3 plugin-kit/pikit.py check plugins/<id>
python3 plugin-kit/pikit.py dev   plugins/<id> --tool <name> --args '{...}'
```

## 下一步

按 [`docs/PLUGIN-PLATFORM-GAP.md`](../docs/PLUGIN-PLATFORM-GAP.md) 的 P0/P1：先让「插件加载状态」
可见，再补 `api.config` 注入与 per-tool 超时，然后把 L1 声明式工具做出来——业务同学用 YAML
就能接一个只读 HTTP 接口，不需要 Python。
