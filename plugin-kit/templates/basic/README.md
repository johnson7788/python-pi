# __PLUGIN_NAME__

__PLUGIN_DESC__

## 文件

| 文件 | 作用 |
|---|---|
| `plugin.json` | manifest：id / version / entry / 权限 / 配置 / 限额 |
| `main.py` | `setup(api)`：注册工具 |

## 本地闭环

```bash
# 1. 校验（会真的 import 并执行 setup）
python3 plugin-kit/pikit.py check .

# 2. 试跑一个工具（参数必须是真的）
python3 plugin-kit/pikit.py dev . --tool hello --args '{"name": "Ada"}'

# 3. 打包（zip 内固定一层 <id>/，宿主安装接口要求）
python3 plugin-kit/pikit.py pack .
```

## 安装

1. 打开「插件」页；
2. 管理员（或单机用户）点「安装插件」，上传 `__PLUGIN_ID__.zip`；
3. 在对应应用上启用；
4. 下一条消息模型就能看到这个工具。

## 改的时候注意

- 工具名在整个部署内唯一，建议加插件前缀（如 `kb_search`）；
- `description` 是模型决定是否调用的唯一依据，写清「何时用」；
- 参数错误、下游失败都返回文本，不要抛异常；
- 联网/写文件/起子进程要如实改 `permissions`，`pikit check` 会用静态扫描对照；
- 密钥不要写进 `plugin.json`，用 `config.*.env` 指向环境变量。
