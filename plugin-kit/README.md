# plugin-kit：python-pi 插件开发工具（MVP）

一个**只用标准库**的小工具，把「写插件」变成四步：

```bash
python3 plugin-kit/pikit.py new  my-plugin        # 1. 从模板创建
python3 plugin-kit/pikit.py check my-plugin       # 2. 校验（会真的执行 setup）
python3 plugin-kit/pikit.py dev   my-plugin --args '{"name":"Ada"}'   # 3. 试跑工具
python3 plugin-kit/pikit.py pack  my-plugin       # 4. 打包成可安装 zip
```

背景与完整路线见 [`docs/PLUGIN-PLATFORM-GAP.md`](../docs/PLUGIN-PLATFORM-GAP.md)。

---

## 为什么需要它

宿主目前的插件加载器只做三件事：读 `plugin.json`、`importlib` 导入、执行 `setup(api)`。
它在**服务端 worker 里**发生，作者在本地看不到任何反馈：

- 少一个 `entry`、工具名重复、事件名写错 → 只在 stderr 留一行；
- `setup()` 抛异常 → 插件静默不生效；
- 工具第一次被调用才暴露的错（比如 `ToolResult(text=...)`）→ 用户看到「工具错误」。

`pikit` 把这一圈搬到本地：**import 你的模块、执行 setup、检查注册物、给一个真实参数试跑**。
它不联网、不启动服务、不依赖 python-pi 之外的包。

## 命令

### `new`

```bash
python3 plugin-kit/pikit.py new kb-search --template http \
  --desc "检索内部知识库，返回标题+摘要+链接"
```

| 模板 | 用途 |
|---|---|
| `basic` | 最小工具；演示 `api.config`、参数校验、错误文本化 |
| `http` | 只读 HTTP 工具；演示超时、响应大小上限、`asyncio.to_thread`、密钥走环境变量 |

生成后 id/name/description 已经写进 `plugin.json`，不需要手工替换占位符。

### `check`

```bash
python3 plugin-kit/pikit.py check my-plugin
python3 plugin-kit/pikit.py check my-plugin --json   # CI 用
```

检查项：

1. **manifest**：`id` 正则、`version`、`apiVersion`、`entry` 是否在目录内；
2. **加载**：能不能 import、有没有 `setup`/`extension`/`register`、`setup()` 是否抛异常；
3. **注册物**：工具名/描述/参数 schema（`required` 是否都有定义）、命令名、事件名是否在宿主会发出的 21 个里；
4. **能力与实现是否一致**：注册了工具但 `capabilities` 没写 `tools` 会警告；
5. **配置**：`config.properties` 的类型/默认值/`required`/`env` 命名，`secret` 是否误写了默认值；
6. **兼容**：`requires.pi` / `requires.python` / `requires.packages` 是否满足；
7. **权限一致性**：静态扫一遍 `.py`，把「声称的权限」和「代码实际 import 的模块 / 调用的写文件函数」对照。

退出码：`0` 通过（允许警告），`1` 有错误，`2` 用法问题。

### `dev`

```bash
python3 plugin-kit/pikit.py dev my-plugin --tool hello --args '{"name":"Ada"}'
python3 plugin-kit/pikit.py dev my-plugin --config '{"greeting":"Hi"}'
python3 plugin-kit/pikit.py dev my-plugin --workspace ~/work/project   # 文件类插件用
```

真的调用工具函数（支持 `(tool_call_id, args, signal, on_update)`、`(args, signal)`、`(args)`、`()` 四种签名，
同步/异步都行），按 `limits.timeoutSeconds` 超时，按 `limits.maxResultChars` 截断。

`--workspace` 决定插件看到的 `api.cwd`（默认当前目录）。文件类插件必须用它——
宿主里 `os.getcwd()` 是所有租户共用的，插件应当用 `workspace_path(api, "相对路径")`。

**这一步价值最大**：参数错了、返回类型错了、只在本机才有的依赖缺失，都会在这里暴露，而不是等用户点。

### `pack`

```bash
python3 plugin-kit/pikit.py pack my-plugin --out dist/my-plugin.zip
```

- 先跑 `check`，有 error 拒绝打包（`--force` 可跳过）；
- zip 内固定一层 `<id>/`——宿主的安装接口要求「zip 里恰好一个 `plugin.json`」；
- 跳过 `__pycache__` / `.git` / `.venv` / `*.pyc`，拒绝符号链接，解压后超过 20MB 拒绝（宿主上限）。

## manifest v2（向后兼容）

运行时不认识的字段会被忽略，所以可以现在就用：

```json
{
  "id": "kb-search",
  "version": "0.1.0",
  "apiVersion": "2",
  "entry": "main.py",
  "capabilities": ["tools"],
  "permissions": {
    "network": ["kb.internal"],
    "filesystem": "none",
    "process": false,
    "secrets": ["KB_API_KEY"]
  },
  "config": {
    "type": "object",
    "properties": {
      "baseUrl": { "type": "string", "default": "https://kb.internal" },
      "apiKey":  { "type": "string", "secret": true, "env": "KB_API_KEY" }
    },
    "required": ["baseUrl"]
  },
  "limits": { "timeoutSeconds": 20, "maxResultChars": 6000 },
  "requires": { "pi": ">=0.1.0", "python": ">=3.10", "packages": [] }
}
```

| 字段 | 谁在读 | 说明 |
|---|---|---|
| `config` | 宿主 + `pikit` | 默认值 / `env` 覆盖由 loader 与 `pikit` 以同一规则注入 `api.config` |
| `limits` | 宿主 + `pikit` | 宿主用 `timeoutSeconds` 给工具加超时；`pikit dev` 用它约束试跑 |
| `permissions` | `pikit` 静态对照（现在） | 未来用于启用确认页与隔离决策 |
| `requires` | `pikit`（现在） | 未来加载前校验，不满足拒绝加载 |

**配置的读取方式**（关键，不要改成第二个参数）：

```python
config = getattr(api, "config", {}) or {}
```

宿主现在只传一个参数，`getattr` 保证同一份代码在「现在」和「注入配置之后」都能跑。

## 与宿主的关系

`pikit` **不是**运行时的一部分。它复刻加载器的规则，但以宿主为准：

- 工具最终由 `packages/pi-coding-agent/src/pi_coding_agent/extensions/loader.py` 加载；
- 安装/启停由 `packages/pi-server/src/pi_server/plugins.py` + `app.py` 管理；
- `pikit` 检查通过不代表宿主一定接受（例如工具名在整个部署内是否唯一，只有部署知道）。

## 已知边界

- 只支持单文件入口（`main.py` 或包内的 `__init__.py`）；不解析相对 import 之外的包结构；
- 静态扫描是启发式：`getattr(os, "system")` 这类规避扫不到；
- 不模拟真实 `ExtensionContext`（session/ui/policy）：`dev` 里调 `ctx.*` 会拿到 `None`；
- 不做依赖安装。

## 下一步（对应路线图 P0/P1）

1. 把 `pikit` 的能力接进 `pi-py plugin ...` 子命令，让作者不需要 clone 仓库；
2. `GET /api/plugins/{id}/status` 把同样的报告搬到 Web；
3. `config` / `limits` / `permissions` 由宿主真正消费；
4. 声明式（L1）工具的 yaml 解析与注册——见路线图 §4.1。
