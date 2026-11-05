# python-pi 原生插件运行时（方向已定）

> 2026-11-03 决策：**不做 DSH / Codex 插件兼容**。插件由使用者用 python-pi 自己的
> Python 插件 SDK 开发，python-pi 负责包格式、安装、启用、会话级挂载与审计。
>
> 这样砍掉了 Node/DSH 兼容层、多 runtime、DSH UI 适配，工作量从“平台级工程”降为“插件系统”。

---

## 0. 结论

- 插件 = **Python 包**：`plugin.json` + Python 入口 `setup(api)` + 可选资源文件。
- 插件运行在 python-pi 进程内（v1），复用现有 `ExtensionAPI` 的事件/工具/命令能力，但增加：
  manifest、版本、权限声明、安装/启停、租户/应用级启用、会话级冻结。
- 切换应用/技能/插件互不影响，靠 **Composition**：
  创建会话时解析出本会话允许的 skills + plugin tools + 基础工具，冻结在会话上；
  旧会话不受后续配置/插件升级影响。
- 单容器多租户可行；但 v1 的插件是**受信任代码**（内部同事开发、管理员安装）。
  若要“租户自助上传任意插件并保证互不影响”，需要把插件执行拆到独立进程/容器（v2）。

---

## 1. 四种东西别再混

| 概念 | 形态 | 能否执行代码 | 隔离方式 |
|---|---|---|---|
| Skill | `SKILL.md` + references/scripts/templates | 脚本通过 bash 执行 | 会话级选择；脚本走现有 sandbox |
| App（Buddy 应用） | 配置：模式/胶囊/模型/绑定 | 否 | Composition，会话冻结 |
| Expert（WorkBuddy） | 提示词包 `plugin.json + agents/*.md` | 否（可选依赖 connector） | 作为 app 资产，会话级提示词 |
| **Plugin（本方案）** | Python 包，注册工具/命令/事件 | 是 | 每会话启用 + 进程内隔离（v1） |

插件回答“新增可执行能力”；应用回答“把这些能力组合成产品”；技能回答“怎么做事”。

---

## 2. 插件包格式

```
my-plugin/
  plugin.json          # 必须
  main.py              # 必须：入口，暴露 setup(api)
  README.md            # 可选
  icon.svg             # 可选
  resources/           # 可选：模板、数据
```

`plugin.json`：

```json
{
  "id": "literature-tools",
  "name": "文献工具",
  "version": "0.1.0",
  "apiVersion": "1",
  "entry": "main.py",
  "author": "internal-team",
  "description": "检索与整理文献的工具集",
  "capabilities": ["tools", "commands", "events"],
  "permissions": {
    "tools": ["read", "grep", "bash"],
    "filesystem": "workspace-read",
    "network": false,
    "secrets": []
  },
  "stateful": false
}
```

约束：

- `id` 小写字母/数字/连字符，全局唯一；`version` 语义化版本。
- `apiVersion` 与 python-pi 插件 API 对齐；不兼容直接拒绝加载。
- `permissions.tools` 必须是平台允许的工具子集；插件不能自己“发明”基础工具。
- `filesystem`：`none | workspace-read | workspace-write`；`network`：true/false。
- `stateful: true` 的插件必须声明；v1 共享进程下按租户隔离状态需要额外约束（见 §5）。
- 入口 `setup(api)` 必须是同步函数；异步初始化放到 `on_start`。

---

## 3. SDK：复用 ExtensionAPI，裁掉危险面

现有 `ExtensionAPI` 已经提供：

- `on(event, handler)`：21 个事件；
- `register_tool(...)` / `register_command(...)`；
- `register_flag(...)` / `register_provider(...)`；
- `ctx.ui`：notify/confirm/select/input/editor；
- `send_message` / `append_entry` / `set_session_name` / `exec` / `set_active_tools`。

插件 SDK 直接复用这套 API，但做三点裁剪：

1. **工具命名空间**：注册的工具对外变成 `plugin__<id>__<name>`，避免冲突。
2. **权限过滤**：`register_tool` 时校验工具能力；`exec` 只能走租户 workspace；`ctx.ui` 只走当前会话通道。
3. **事件白名单**：v1 允许 `tool_call`、`tool_result`、`before_agent_start`、`input`、`message_end`、
   `session_before_switch/fork`、`agent_start/end`、`turn_start/end`、`tool_execution_*`；
   provider/compaction/session_tree 等内部事件不开放，避免插件依赖未稳定接口。

示例：

```python
from pi_plugin import PluginAPI, ToolResult

def setup(api: PluginAPI):
    @api.tool(
        name="search_literature",
        description="按 PICO 检索文献",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        permission="workspace-read",
    )
    async def search(args, ctx):
        # ctx.workspace / ctx.tenant / ctx.signal
        return ToolResult(text="...")

    @api.on("before_agent_start")
    async def inject(event, ctx):
        event.system_prompt += "\n## 文献工具\n..."
```

开发体验：

```bash
pi-py plugin init my-plugin        # 生成目录、manifest、示例工具
pi-py --plugin-dir ./my-plugin     # 本地加载（仅开发）
pi-py plugin validate ./my-plugin  # 校验 manifest / 导入 / 工具 schema
```

---

## 4. 安装、启用、会话冻结

### 4.1 生命周期

```
开发目录 → validate → 打包 zip → 管理员安装 → active_version
       → 租户/应用 enable → 会话 Composition 挂载 → 使用 → 升级/停用
```

- 安装到 `<data_root>/plugins/<id>/<version>/`；同一 id 只有一个 `active_version`。
- 安装/启用不执行插件代码之外的副作用；`validate` 只导入入口并读 manifest（不做真实调用）。
- 升级：替换 `active_version`；**新会话**用新版本；旧会话保持已加载实例直到结束。
- 停用：新会话不再挂载；旧会话继续（管理员可强制终止）。

### 4.2 谁能安装

v1 建议：**管理员安装，租户只能启用/停用**。
理由：插件是进程内 Python 代码，租户自助上传等于给每个同事一个以服务进程权限运行的入口；
团队模式当前本来就禁用了用户扩展（`app.py:545`）。

如果产品上必须“用户自己上传”，则 v1 必须改成每插件独立进程（见 §5），否则不能上线。

### 4.3 会话 Composition

```json
{
  "appId": "research-workbench",
  "modeId": "clinical-review",
  "capsuleId": "extract-pico",
  "skillIds": ["literature-screening"],
  "pluginIds": ["literature-tools"],
  "pluginVersions": {"literature-tools": "0.1.0"},
  "tools": ["read", "grep", "plugin__literature-tools__search_literature"],
  "permissions": {"filesystem": "workspace-read"}
}
```

- 创建会话时解析一次，写 `composition_hash`；后续不变。
- 插件的工具只有进入 `tools` 才暴露给模型。
- 同一租户的不同应用可以启用不同插件集，互不影响；这就是“切换应用插件不串”的落点。

---

## 5. 隔离与信任模型（必须写清楚）

v1 插件与 server 同进程，等价于现有扩展的信任级别：

| 风险 | v1 现状 | 缓解 |
|---|---|---|
| 插件抛异常 | 被 runner 捕获，不打断循环 | 已有 |
| 插件死循环 / `os._exit` | 可能拖垮服务 | 代码审核、工具超时、禁止插件自建线程/进程（文档约束） |
| 插件读其他租户文件 | 同进程同权限，**防不住** | 只允许管理员安装可信插件；后续拆进程 |
| 插件拿全局密钥 | 进程环境可见 | 不把密钥放 env；按调用下发；插件权限声明 |
| 插件跨租户状态 | 共享进程会串 | 要求 `stateful:false`；或按 `ctx.tenant` 分区；v2 每租户进程 |

**v1 的承诺应是“行为隔离 + 崩溃可恢复”，不是“安全隔离”。**
如果要对同事承诺“插件绝对读不到别人的数据”，必须上 v2：

- 每插件一个 Python 子进程（JSON-RPC）；工具调用跨进程；
- 每租户一个 host，或每租户独立 OS 用户；
- 只把 workspace 路径与短时凭据传给 host；插件不能直接访问 server 内存与全局环境。

v2 的代价：事件/hook 需要跨进程回调，延迟与复杂度上升；建议只把 **tools** 放子进程，
hooks 仍留在受信进程，或 v2 只支持 tools 型插件。

---

## 6. 数据模型与 API

```sql
plugins(id, name, description, author, latest_version, active_version, status, manifest_json, created_at)
plugin_versions(plugin_id, version, path, hash, api_version, installed_at, status)
plugin_enable(tenant_id, app_id NULL, plugin_id, enabled, updated_at)
plugin_audit(id, ts, tenant_id, plugin_id, action, detail)
```

API：

```
GET    /api/plugins                         已安装（含版本、权限、启用范围）
POST   /api/plugins/install                 admin：zip 或目录路径
POST   /api/plugins/{id}/activate           admin：切换 active_version
PATCH  /api/plugins/{id}/enable             {tenant_id, app_id?, enabled}
GET    /api/plugins/{id}/logs              最近日志（脱敏）
DELETE /api/plugins/{id}                    仅在无会话引用时
```

开发侧 CLI：

```
pi-py plugin init / validate / pack
pi-py --plugin-dir ./my-plugin
```

---

## 7. 与现有 extensions 的关系

- **不混用目录**：`extensions/` 继续是平台内置治理扩展（policy/审计/内部工具），启动即加载；
  `plugins/` 是用户开发的可安装插件，按租户/应用启停。
- **复用 API，不复用加载器**：插件加载器要支持 manifest、版本、启用过滤，而现有
  `load_extensions()` 是启动时一次性 import、无版本/权限。
- 迁移路径：先把 `ExtensionAPI` 抽成 `PluginAPI` 协议；现有扩展可以继续用旧加载器，
  新插件走新加载器；未来可把内置扩展也包装成插件。

---

## 8. 分期与工时

| 阶段 | 交付 | 工时 |
|---|---|---|
| P0 | `plugin.json` + SDK（PluginAPI 包装 ExtensionAPI）+ 插件加载器 + 命名空间 | 5–7 人日 |
| P0 | 插件表/版本表 + 安装/启用 API + 会话 Composition 挂载 | 5–7 人日 |
| P0 | `pi-py plugin init/validate/pack` + `--plugin-dir` 开发模式 | 3–5 人日 |
| P1 | 插件管理页（列表/版本/权限/启用/日志） | 5–7 人日 |
| P1 | 工具超时/异常隔离/审计/卸载保护 | 3–5 人日 |
| P2 | 每插件子进程 host（tools-only）+ 每租户 host（可选） | 8–12 人日 |
| P2 | 管理员上传 zip + 审核流（可选） | 3–5 人日 |

v1（P0+P1）约 **3–4 周**，能支持内部同事开发、管理员安装、按应用启用、会话级隔离。
如果要求用户自助上传，至少要把 P2 的进程隔离一起做，v1 不能承诺安全隔离。

---

## 9. 还需要确认的两件事

1. **安装权限**：管理员安装，还是允许租户自助上传？
   - 管理员安装：v1 可以进程内，3–4 周；
   - 自助上传：必须加每插件子进程，约 +2 周，且仍不是强安全隔离。
2. **插件是否需要 UI**：v1 只支持工具/命令/对话框（`ctx.ui`），不支持注册前端组件/侧栏页面。
   如果需要插件贡献界面，需要另做 client plugin 协议，成本另计。
