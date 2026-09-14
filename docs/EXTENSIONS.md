# Python 扩展协议

这是 pi 的 TypeScript 扩展系统在 Python 侧的重设计。**不追求跨语言兼容**，但尽量保持
概念、事件名与合并语义一致，以便 pi 的文档和示例仍然可读。

## 一个扩展长什么样

扩展就是一个普通 Python 模块，暴露 `setup(pi)` 工厂（同步或异步均可）：

```python
# .pi/extensions/guard.py
from pi_coding_agent.extensions import InputEventResult, ToolCallEventResult


def setup(pi):
    pi.on("tool_call", guard)
    pi.on("input", expand)
    pi.register_tool(name="greet", description="Greet someone")(greet)
    pi.register_command("status", "Show the session", status)


def guard(event, ctx):
    if event.tool_name == "bash" and "rm -rf" in (event.input or {}).get("command", ""):
        if not ctx.has_ui:
            return ToolCallEventResult(block=True, reason="refusing to run rm -rf")
        # Blocking dialogs are only available where a user can answer.
        return ToolCallEventResult(block=True, reason="blocked")
    return None


def expand(event, ctx):
    return InputEventResult(action="transform", text=event.text.replace("XX", "world"))


async def greet(args):
    return f"hello {args['name']}"


async def status(args, ctx):
    ctx.ui.notify(f"{len(ctx.messages)} messages in {ctx.session_id}")
```

入口点按 `setup` → `extension` → `register` 的顺序查找。`register_tool` 与
`register_command` 也可直接当装饰器用。

## 从哪里加载

按 pi 的发现顺序，先全局后项目，最后是指令行/设置里显式给出的路径：

| 位置 | 作用域 | 信任要求 |
|---|---|---|
| `<agent_dir>/extensions/*.py` | 全局 | 无 |
| `<agent_dir>/extensions/<name>/__init__.py` | 全局 | 无 |
| `<project>/.pi/extensions/*.py` | 项目 | **需要项目被信任** |
| `<project>/.pi/extensions/<name>/__init__.py` | 项目 | **需要项目被信任** |
| `-e/--extension <path>` | 本次运行 | 由调用者负责 |
| 已安装包声明的 entry point 组 `pi_coding_agent.extensions` | 全局（类比 pi packages） | 无 |

只递归一层（对应 pi 的 `<name>/index.ts`），按 `realpath` 去重，顺序即加载顺序——顺序是
可观察的，因为多个扩展的返回值会按顺序合并。

## 安全模型

**没有沙箱，也没有权限系统——与 pi 一致。** pi 的文档写得很直白：扩展以你的完整系统权限运行，
只应从你信任的来源安装。本移植版保持这个模型，并保留 pi 唯一的门控：**项目本地扩展只在项目
被信任后加载**。

换句话说：`-e` 一个陌生文件、或信任一个陌生项目，等同于让它以你的权限执行任意代码。

## 事件

事件分三类，**合并语义各不相同**，这是最容易写错的地方：

| 事件 | 语义 |
|---|---|
| `input` | 链式：`action="transform"` 改写文本并继续，`action="handled"` 立即停止处理（该 prompt 被消费，不发给模型） |
| `before_agent_start` | `system_prompt` 链式（后一个扩展看到前一个的结果）；`messages` 累加注入 |
| `tool_call` | 参数**原地修改** `event.input`；第一个返回 `block` 的立即短路；抛异常的处理器等同于 block（安全方向） |
| `tool_result` | 链式：每个处理器看到上一个的输出，可改 `content`/`details`/`is_error` |
| `message_end` | 链式：可替换消息，但**角色必须不变**，否则被拒绝并记为错误 |
| `session_before_switch` / `session_before_fork` | 可 `cancel`，第一个 cancel 生效 |
| 其余（`agent_start` / `turn_end` / `tool_execution_*` / `model_select` …） | 纯通知，返回值被忽略 |

### 已发出的事件（21 个）

`session_start` `session_shutdown` `session_info_changed` `session_before_switch`
`session_before_fork` `input` `before_agent_start` `agent_start` `agent_end` `turn_start`
`turn_end` `message_start` `message_update` `message_end` `tool_execution_start`
`tool_execution_update` `tool_execution_end` `tool_call` `tool_result` `model_select`
`thinking_level_select`

### 暂不发出

pi 还有这些事件，本移植版不发出（原因见括号）：

- `project_trust` / `resources_discover`（在扩展加载之前发生，鸡生蛋问题；pi 用两阶段加载解决）
- `before_provider_request` / `before_provider_headers` / `after_provider_response`（需要把钩子下沉到 provider 层）
- `session_before_compact` / `session_compact` / `session_compact_failed` / `session_before_tree` / `session_tree`
- `agent_settled`、`ui_prompt_start` / `ui_prompt_end`、`user_bash`（TUI 或用户 bash 专属）

`EMITTED_EVENTS` 常量是这个集合的权威来源。

## `ctx.ui`

`ctx.has_ui` 告诉扩展"问用户"是否有意义。各模式下的实现：

| 模式 | `has_ui` | 行为 |
|---|---|---|
| `rpc` | `True` | 走 pi 的 `extension_ui_request` / `extension_ui_response` 子协议；`notify`/`setStatus`/`setTitle` 是即发即忘，`select`/`confirm`/`input`/`editor` 会阻塞等待应答 |
| `print` / `json` | `False` | `notify` 写到 stderr；对话框返回安全默认值（`None` / `False`），不阻塞 |

超时（秒）到期、收到 `cancelled`、或会话结束时，阻塞的对话框都以安全默认值解除；
`cancel_all()` 保证关闭时不会有处理器永远挂住。

## 错误隔离

**处理器抛异常永远不会打断 agent 循环。** 异常被记录成 `ExtensionError` 并继续调用下一个处理器；
在 RPC 模式下同时以 `extension_error` 事件发给客户端。

一个例外是有意的：`tool_call` 处理器抛异常被视为 **block**——安全方向是"不确定就不要执行"。

## 与 pi 的差异

| 项 | pi | 本移植版 |
|---|---|---|
| 语言/加载 | TypeScript，`jiti` 同进程编译加载 | Python，`importlib` 直接导入 |
| 跨语言互通 | — | **不支持**。pi 的扩展返回活的 TUI 组件、依赖同进程 `AbortSignal` 和参数原地修改，无法跨语言边界存活 |
| TUI 渲染器 | `registerMessageRenderer` / `registerEntryRenderer` / `registerShortcut` / `registerMarkdownTransformer` / `setWidget` / `setFooter` / `setHeader` / 自定义组件 | 不提供（没有 TUI）；`ctx.ui` 只保留无头可用的部分 |
| 快捷键 / 编辑器 | 完整编辑器控制 | 不提供 |
| 异步工厂 | 顶层 await | 支持，但必须在事件循环启动**之前**加载（否则报明确错误） |
| 包管理 | `pi install`（npm/git） | 用 Python 生态的 entry point，不做安装器 |
| 权限 | 无沙箱 | 同样无沙箱 |

## 已知边界

- **`input` 事件只在 `prompt()` 路径触发**；扩展自己注入的消息（`send_message`）不再经过 `input`。
- **`send_message` 的即时投递是尽力而为**：循环在回合边界读取消息列表，运行中途注入不保证本回合可见。
  需要确定性时用 `deliver_as="steer"` 或 `"followUp"`。
- **重复的命令名不会被覆盖**，第二个会变成 `name:2`，让冲突可见而不是静默丢一个。
- 扩展工具默认全部启用（pi 需要显式列在 `tools` 里才启用）。
