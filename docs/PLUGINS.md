# python-pi 插件 SDK（v1 + v2 元数据）

插件是可执行的 Python 代码，由管理员安装到部署的插件目录，再按租户/应用启用。
它和 Skill 不同：Skill 是提示词与流程，插件新增可执行工具/命令。

> 本地开发用 [`plugin-kit/`](../plugin-kit/README.md)：`new` / `check` / `dev` / `pack`。
> 完整对比与路线图见 [`PLUGIN-PLATFORM-GAP.md`](PLUGIN-PLATFORM-GAP.md)。

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
  "apiVersion": "2",
  "entry": "main.py",
  "description": "最小示例",
  "capabilities": ["tools"],
  "permissions": {
    "tools": [],
    "filesystem": "none",
    "network": false,
    "process": false,
    "secrets": []
  },
  "config": {
    "type": "object",
    "properties": {
      "greeting": { "type": "string", "default": "你好", "description": "问候语前缀" }
    }
  },
  "limits": { "timeoutSeconds": 30, "maxResultChars": 20000 },
  "requires": { "pi": ">=0.1.0" }
}
```

- `id`：小写字母/数字/连字符，全局唯一，≤41 字符；
- 同一 id 只有一个版本（当前不做多版本并存）；
- `entry` 必须是目录内文件，不能逃逸；
- `permissions`：**当前是声明，v1/v2 运行时都不强制执行**；插件与 server 同进程、同权限，
  所以「启用插件」等于「授权它使用部署进程的全部权限」。只安装审核过的代码；
- `config`：加载时解析，默认值 + 环境变量注入 `api.config`（见下）；
- `limits.timeoutSeconds`：加载时写到该插件每个工具的 `timeout_seconds`，由 agent 循环执行；
  `limits.maxResultChars` 目前只由 `plugin-kit` 的 `dev` 使用，宿主不强制；
- `requires`：仍未被宿主消费，只用于 `pikit check`（P1）。

## 入口

```python
from pi_coding_agent.plugins import PluginAPI


def setup(api: PluginAPI) -> None:
    # 宿主当前只调用 setup(api)；config 用 getattr 读，向前兼容。
    config = getattr(api, "config", {}) or {}
    prefix = str(config.get("greeting") or "你好")

    @api.register_tool(
        name="hello_plugin",
        description="Return a greeting.",
        parameters={"type": "object", "properties": {}},
    )
    async def hello_plugin(tool_call_id, args, signal, on_update):
        return f"{prefix}，world！"     # 直接返回字符串最简单
```

`PluginAPI` 与 `ExtensionAPI` 是同一套注册面：`register_tool`、`register_command`、
`on(event, handler)`、`register_flag`、`register_provider` 等。工具名要在整个部署内唯一
（v1 不做命名空间包装）。

## 配置

宿主加载插件时读取同目录 `plugin.json` 的 `config.properties`：

```json
{
  "config": {
    "type": "object",
    "properties": {
      "baseUrl": { "type": "string", "default": "https://kb.internal" },
      "apiKey":  { "type": "string", "secret": true, "env": "KB_API_KEY" }
    }
  }
}
```

- `default` 是默认值；
- `env` 指向的环境变量存在时覆盖默认值——**密钥只走这条**，不要写进 manifest；
- 结果在 `setup(api)` 里通过 `api.config` 读取（扩展没有 manifest，`api.config` 是 `{}`）。

宿主不校验 `config` 的类型/必填（`pikit check` 会），因为校验失败时应当降级为默认值而不是
让整个 worker 起不来。

**返回值的正确形状**：`return "文本"` 就够了，运行时会包成 `TextContent`。
也可以返回 `ToolResult`，但它是 `pi_agent.AgentToolResult`，字段是
`content`/`details`/`usage`/`terminate`/`added_tool_names`——**没有 `text=` 参数**：

```python
from pi_ai import TextContent
from pi_coding_agent.plugins import ToolResult

return ToolResult(content=[TextContent(text="...")])
```

## 启用

- 插件默认不启用；
- 插件页按当前应用启停，写入 `plugin_enable(uid, app_id, plugin_id)`；
- 启用/停用会驱逐该应用/模式的 worker，下一条消息使用新插件集；
- 不同应用可以用不同插件集；同名 worker 的 key 含 app/mode，互不影响。

## 安装与投稿

管理员两条安装路径：

- 插件页「安装插件」上传 zip（≤10MB，解压 ≤20MB，zip 内恰好一个 `plugin.json`）；
- 或从「市场」安装内置条目。

团队成员不必找管理员要权限：在插件页点「提交插件」上传 zip，进入**待审队列**
（`<data_root>/plugin-submissions/`），管理员在「待审」页点「安装」后才会进入
`<data_root>/plugins/`。「驳回」只删除投稿，不动已安装的插件。

卸载会删除目录、所有租户的启用记录，并驱逐已加载它的 worker。

## 加载状态

插件页每个条目会显示实时状态：`已加载 N 个工具：…`、`尚未加载（下一条消息生效）`、
或 `加载失败：<原因>`。数据来自 `GET /api/plugins/status?app=<app>`，由当前 worker 的
`ExtensionRunner` 快照 + 加载诊断组成——插件没生效时不用再翻服务端 stderr。

当前限制：产品只做**团队形态**；安装只允许管理员（上传 zip 或从市场装），普通用户只能启停。
作者写好插件后交给管理员，或走后续的「投稿箱」流程（见路线图 P1）。

## 本地开发

```bash
python3 plugin-kit/pikit.py new my-plugin --template basic
python3 plugin-kit/pikit.py check my-plugin
python3 plugin-kit/pikit.py dev my-plugin --args '{"name":"Ada"}'
python3 plugin-kit/pikit.py pack my-plugin
```

`check` 会真的 import 模块、执行 `setup()`、校验工具 schema 与权限声明；
`dev` 用真实参数调用一次工具——绝大多数「装上才发现」的错误会在这里暴露。

## 工作区路径

插件跑在服务端进程里，**`os.getcwd()` 是所有租户共用的**，不能用它找用户文件。加载时宿主会把
当前 worker 的工作区放到 `api.cwd`，用 SDK 提供的解析函数：

```python
from pi_coding_agent.plugins import workspace_path

path = workspace_path(api, "data/patients.csv")   # 越界（../、绝对路径）直接 ValueError
```

普通工具（`read`/`write`/`bash`）已经绑定到同一个工作区，插件只是需要自己取。

## 示例

- [`examples/plugins/hello-pi`](../examples/plugins/hello-pi)：最小可运行示例（20 行）。
- [`plugins/`](../plugins/README.md)：官方起点包，四个可以直接用的插件
  （临床计算器 / PubMed / 网页抓取 / CSV 工具），同时是四种典型写法的样板。

## 限制

- 进程内执行，崩溃仍可能影响服务；per-tool 超时只覆盖 `limits.timeoutSeconds` 声明的插件，
  取消的是 await，已经交给线程的阻塞调用会跑完才退出；
- 没有审核/签名/来源校验流程，只在安装时校验 manifest 与路径安全；
- 没有热加载；启停后由 worker 重建生效（会中断进行中的对话）；
- 工具没有命名空间前缀，作者需保证唯一；
- 不支持插件贡献前端界面；
- 没有依赖声明与安装：插件用到的第三方包必须已经在运行环境里。
