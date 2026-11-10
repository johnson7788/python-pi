# PubMed 检索（pubmed）

两个只读工具，走 NCBI E-utilities，**不需要 API key**（有 key 就把限流从 3 次/秒提到 10 次/秒）。

| 工具 | 作用 |
|---|---|
| `pubmed_search` | 关键词/布尔式检索，返回 PMID、标题、期刊、年份、前三位作者、DOI |
| `pubmed_abstract` | 按 PMID 取结构化摘要（按 Background/Methods/Results 等小节） |

## 为什么做成插件而不是 Skill

Skill 只能「告诉模型去 curl」；插件把**检索式构造、限流、错误处理、字段裁剪**固定下来：

- 一次调用最多 10 条，摘要一次一篇——不会把整页 XML 塞进上下文；
- 遵守 NCBI 的 `tool` + `email` 约定，内置串行限流（无 key 3 次/秒）；
- 网络失败返回可诊断文本，不抛异常。

## 安装

1. 管理员上传 `pubmed.zip` 并启用；
2. 建议在部署环境设置 `NCBI_EMAIL`（NCBI 要求）；有 key 再设 `NCBI_API_KEY`。

## 调用示例

```text
查一下 2023 年以来 SGLT2 抑制剂在心衰里的大型 RCT，给我 5 篇。
```

```text
读一下 PMID 38000000 的摘要，按小节总结。
```

## 边界

- 只读检索与摘要，不下载全文（全文有版权与访问限制）。
- 不做证据等级判断；那是 Skill + 人工的事。
- NCBI 偶发 429/5xx 会重试一次，仍失败就返回状态码。
