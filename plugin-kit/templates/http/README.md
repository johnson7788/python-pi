# __PLUGIN_NAME__

__PLUGIN_DESC__

## 文件

| 文件 | 作用 |
|---|---|
| `plugin.json` | manifest：权限声明 `network: true`，配置 `baseUrl` / `apiKey` |
| `main.py` | `setup(api)`：一个只读 HTTP 工具 |

## 本地闭环

```bash
export __PLUGIN_ENV__=your-token

python3 plugin-kit/pikit.py check .
python3 plugin-kit/pikit.py dev . --tool http_lookup --args '{"query": "乳腺癌"}'
python3 plugin-kit/pikit.py pack .
```

`apiKey` 声明了 `"secret": true, "env": "__PLUGIN_ENV__"`：`pikit` 与宿主都不会把
它写进 manifest，只从环境变量读。

## 上线前检查

- `base_url` 必须是内网或你信任的域名；不要允许用户把 `query` 拼进路径；
- 下游返回可能很大：保留 `MAX_RESPONSE_BYTES` 与 `MAX_RESULTS`；
- 错误信息里不要带 token、堆栈、内部 IP；
- 只读接口先上；写接口要单独做审批与幂等；
- 结果条数/长度按模型上下文预算裁，别把整个文档塞回去。
