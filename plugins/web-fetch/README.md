# 网页抓取（web-fetch）

一个只读工具：`web_fetch(url, max_chars=6000, format=text|json)`。

## 它解决什么

模型要读一个网页时，现在只能 `bash` + `curl`：拿到一坨 HTML、可能几百 KB、还可能卡住。
`web_fetch` 把「抓取 → 去噪 → 裁剪」固定成一步：

- 只允许 `http`/`https`，**拒绝内网/回环/链路本地地址**（防 SSRF），重定向逐跳复检；
- 最多 1MB、20 秒、5 次重定向；
- HTML 去 `script/style/nav/footer/form`，标题单独提取，块级标签转换行；
- `application/json` 自动格式化；非文本类型直接说明不支持。

## 安装

管理员上传 `web-fetch.zip` 并启用。无配置项。

## 调用示例

```text
把这个指南页面的用药部分读一下：https://example.org/guideline
```

## 边界与风险

- **DNS rebinding 仍有残余风险**：这里在请求前解析域名并校验 IP，但 DNS 在请求时可再次变化。
  部署在内网、且不希望模型访问外网时，应在网络层（容器出网策略）禁用，而不是依赖本工具。
- 只做 GET，不发送任何凭据；不会带 cookie。
- 反爬/登录墙页面会返回登录页文本，需要人判断。
- 结果按字符截断；长文档建议分段抓目录页或改用专门的数据源。
