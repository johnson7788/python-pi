# python-pi 插件 SDK（v1）

插件是可执行的 Python 代码，由管理员安装到部署的插件目录，再按租户/应用启用。
它和 Skill 不同：Skill 是提示词与流程，插件新增可执行工具/命令。

## 目录

```
<data_root>/plugins/hello-pi/
  plugin.json
  main.py
```

`plugin.json`：

```json
{
  "id": "hello-pi",
  "name": "Hello Pi",
  "version": "0.1.0",
  "apiVersion": "1",
  "entry": "main.py",
  "description": "最小示例",
  "capabilities": ["tools"],
  "permissions": {"tools": [], "filesystem": "none", "network": false}
}
```

- `id`：小写字母/数字/连字符，全局唯一；
- 同一 id 只有一个版本（当前不做多版本并存）；
- `entry` 必须是目录内文件，不能逃逸；
- `permissions` 当前是声明，v1 不强制执行；插件与 server 同进程、同权限，只安装可信代码。

## 入口

```python
from pi_coding_agent.plugins import PluginAPI, ToolResult

def setup(api: PluginAPI) -> None:
    @api.register_tool(
        name="hello_plugin",
        description="Return a greeting.",
        parameters={"type": "object", "properties": {}},
    )
    async def hello_plugin(tool_call_id, args, signal, on_update):
        return ToolResult(text="hello")
```

`PluginAPI` 与 `ExtensionAPI` 是同一套注册面：`register_tool`、`register_command`、
`on(event, handler)` 等。工具名要在整个部署内唯一（v1 不做命名空间包装）。

## 启用

- 插件默认不启用；
- 插件页按当前应用启停，写入 `plugin_enable(uid, app_id, plugin_id)`；
- 启用/停用会重启该应用/模式的 worker，下一条消息使用新插件集；
- 不同应用可以用不同插件集；同名 worker 的 key 含 app/mode，互不影响。

## 示例

[`examples/plugins/hello-pi`](../examples/plugins/hello-pi) 是最小可运行示例。

## 限制

- 进程内执行，崩溃/死循环可能影响服务；只安装审核过的代码；
- 没有上传/审核/签名流程，安装 = 把目录放到 `<data_root>/plugins`；
- 没有自动热加载；启停后由 worker 重建生效；
- 工具没有命名空间前缀，作者需保证唯一；
- 不支持插件贡献前端界面。
