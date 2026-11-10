# python-pi 插件平台 vs DeepSeek Harness：差距、取舍与「用户自服务」路线

> 基准日：2026-11-10。
> A = `deepseek-harness` 0.2.2-alpha.2（Developer Preview，Node ≥ 24.9，pnpm 11.28.5）。
> B = 本仓库 `python-pi` 0.1.0。
> 本文只讨论**插件这一层**：一个开发者（或用户）从零到「模型真的调用我的工具」，再到「别人也能装、能升级、能卸载」。工具面、多 agent、会话、模型层的差距见 [`GAP-ANALYSIS-vs-DSH.md`](GAP-ANALYSIS-vs-DSH.md)，不在这里重复。

---

## 0. 一页结论

1. **差的不是插件 API，是 API 外面的那一圈。** 写一个工具，B 比 A 更短：A 要 `defineTool` + `output.schema` + `render` + Schemastery 配置 + Cordis `inject`；B 只要一个 `setup(api)` 加一个装饰器。真正的差距在**契约、生命周期、打包、分发、开发工具链、治理**这六件事上。
2. **分发与开发体验是两处结构性缺失（🔴）。** A 有 `dsh plugin add`（直通 pnpm）、`engines.dsh` 兼容范围、profile/patch 分层、远端插件索引（含版本选择、图标、分类）；B 只有「管理员上传 zip」+ 一份写死在 `market.py` 里的目录（本次补到 9 条），改一条要重启发版。
3. **B 有一个 A 没有的机会：用户不是开发者。** A 假设作者会写 TypeScript、会用 pnpm、能读 2000 篇文档。B 的用户是业务/团队用户。所以正确目标不是「教会用户写 Python」，而是**让用户不需要写 Python 也能得到插件**——把扩展分成「Skill → 声明式工具 → Python 插件」三级，再用产品自己的 agent 把前两级的生产自动化。
4. **一个已经修掉的硬伤 + 一个流程缺口**：①（已修）唯一可抄的示例 `hello-pi` 里 `ToolResult(text=...)` 会直接抛 `TypeError`（`examples/plugins/hello-pi/main.py:24`，实测），文档同错；② 团队里「作者 → 管理员」没有交接通道——安装只有管理员上传 zip 一条路，没有投稿/待审/来源信息，也没有版本回滚。
5. **安全上 B 不是「更差」，而是「风险形状不同」**：A 的插件同样是宿主权限的可信代码（官方明确未审计），但 A 有工具级审批/沙箱/权限预设；B 的插件在多租户里被当作 **builtin** 加载（`bootstrap.py:341` + `app.py:649`），即使用户扩展被禁用，管理员装的插件仍能读所有租户的会话。

### 差距速查

| 维度 | A：DSH | B：python-pi | 等级 |
|---|---|---|---|
| 工具注册 | `ctx.tools.register(defineTool(...))` | `@api.register_tool(...)` | 🟢 B 更短 |
| 参数/输出契约 | 参数 schema + `output.schema` + `render` | 只有参数 schema，返回值不校验 | 🟠 |
| 配置 | Schemastery schema + 默认值 + 校验 | 无（改代码或改环境变量） | 🟠 |
| 依赖注入 / 加载顺序 | `inject` 声明，服务就绪才 `apply` | 目录顺序，谁先 import 谁先跑 | 🔴 |
| 生命周期 / 清理 | Fiber + `ctx.effect()` 自动撤销 | 无 teardown，worker 退出即丢弃 | 🔴 |
| 热更新 | HMR（卸载→重载→重跑 apply） | 无；启停靠重建 worker（会中断对话） | 🟠 |
| 扩展形态 | 工具/服务/事件/模型适配器/客户端 UI | 工具/命令/事件/flag（provider 是空缝） | 🟠 |
| 版本与兼容 | npm 语义化版本 + `engines.dsh` + 可回滚 | 同 id 单版本，无兼容声明 | 🔴 |
| 打包与依赖 | npm 包 + `dsh.bundle` + pnpm 解析依赖 | 目录 + `plugin.json`，无依赖声明 | 🔴 |
| 安装 | `dsh plugin add`（CLI，可 git/npm） | 管理员上传 zip，仅团队模式 | 🔴 |
| 作用域 | profile 级，可并存多套组合 | 部署级 plugin_root + 按用户/应用启用 | 🟠 |
| 分发 | 插件索引 JSON + Discover 页 + 版本选择 + 生成器 | `market.py` 里 9 条硬编码，重启生效 | 🔴 |
| 开发者工具 | 2000+ 篇文档、cookbook、`--patch` 热挂载、`dump-config`、tool catalog 生成 | 2 篇文档（共 209 行）、1 个示例、无脚手架/校验/本地跑 | 🔴 |
| 执行隔离 | 插件代码不隔离；**工具**有审批/沙箱 | 插件与工具都不隔离；无强制超时 | 🟠 |
| 可观测 | 事件、telemetry、诊断包 | 加载诊断只到 stderr，UI 看不到 | 🟠 |
| 自举 | `plugin_manager` / `plugin_market` 工具，agent 可自己装 | 无 | 🟠 |

---

## 1. 两套模型的最小对照

### A：Cordis 插件（TypeScript）

```ts
export const name = 'greet-tool'
export const inject = ['tools']
export function apply(ctx: Context, config: Config) {
  ctx.tools.register(defineTool({ name: 'greet', description: '…', parameters: {…},
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) { return `${config.prefix}, ${args.name}!` } }))
}
```

### B：python-pi 插件（Python）

```python
def setup(api):
    @api.register_tool(name="greet", description="…", parameters={…})
    async def greet(tool_call_id, args, signal, on_update):
        return f"你好，{args['name']}！"
```

### 概念映射

| A | B | 差别 |
|---|---|---|
| `Plugin` 模块（`name`/`inject`/`apply`） | 模块里的 `setup(api)` | B 没有 `name`、没有 `inject` |
| `Context`（服务容器） | `ExtensionContext`（session/ui/policy） | B 的 ctx 是**会话视图**，不是服务注册表 |
| `Service` | 无 | B 的 `register_provider` 没有消费方（`extensions/api.py:238`） |
| `Fiber` / `ctx.effect()` | 无 | B 没有卸载与资源回收 |
| `Config` + Schemastery | 无 | B 的 `setup` 只收到 `api`（`extensions/loader.py:335-337`） |
| `dsh.bundle` + `cordis.patch.yml` + profile | `plugin.json` + 目录 | B 没有「配置层」概念 |
| `engines.dsh` | 无 | B 不检查兼容性 |
| `dsh.client` | 无 | B 不支持插件贡献界面 |

> 一句话：A 是**运行时框架**，B 是**进程内注册表**。B 的轻不是缺点——对 Python 作者反而更好用；但它把「组合、生命周期、分发」这些平台责任留给了宿主，而宿主目前没接。

---

## 2. 九维对比

### 2.1 运行时与生命周期 🔴

**A**：`inject` 声明依赖 → Cordis 等 `tools` 服务就绪才执行 `apply`；Fiber 状态机（PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED）；所有 `ctx.on` / `ctx.tools.register` / `ctx.effect` 注册都归属当前 Fiber，卸载时**逆序**撤销；服务提供方消失时依赖它的插件自动卸载、恢复后重载；可选 HMR。

**B**：`importlib` 直接导入，`factory(api)` 同步或 async 跑一次（`extensions/loader.py:328-343`）；注册挂在 `ExtensionAPI` 的 dict 上；没有卸载钩子，没有清理顺序；插件随 worker 存活，启停 = `registry.evict()` 重建 worker（`app.py:787-799`、`1976-1994`）。

**用户可感知后果**：

- 插件改一行 → 整轮对话的 worker 被换掉，正在进行的对话中断；
- 插件开的连接/线程/定时器没有地方关，反复启停会累积；
- 两个插件不能声明「我要用另一个插件提供的能力」，只能 import 对方模块——耦合且容易循环。

**补法（3–5 人日）**：`api.effect(fn) -> disposer`、`api.on_dispose(cb)`；runner 在 worker 关闭时逆序执行；`api.requires = ["name"]` 做最小依赖声明（不引入完整 DI）。HMR 不必做。

### 2.2 契约：参数 / 输出 / 配置 🟠

**A**：参数 schema 由运行时校验；`output.schema` 声明返回值形状，`render` 决定模型看到什么；配置用 Schemastery 声明类型/默认值/必填，加载时校验并注入 `apply(ctx, config)`。

**B**：只有参数 schema（模型侧），**返回值不校验**；没有 output 声明；`setup(api)` 拿不到配置（`extensions/loader.py:335`）。

**用户可感知后果**：

- 插件返回 `None`、抛异常、返回 10MB 文本，运行时都照单全收，模型侧表现是「工具坏了但没人知道为什么」；
- 同一个插件换个环境（测试/生产、不同医院）要 fork 一份代码；
- 密钥只能硬编码或读 `os.environ`，没有「声明式密钥」这一层。

**补法（1–2 人日）**：

- `plugin.json` 增加 `config`（JSON Schema 子集）与 `limits`；
- loader 从 manifest 解析默认值，叠加部署覆盖，挂到 `api.config`（**不改 `setup` 签名**，向后兼容）；
- 工具返回值做「能渲染就渲染，不能就 `str()` + 警告」，`limits.maxResultChars` 截断。

### 2.3 能力面：工具 / 服务 / 事件 / 模型 / UI 🟠

| 形态 | A | B | 说明 |
|---|---|---|---|
| Tool | ✅ | ✅ | B 的装饰器更顺手 |
| Command | 有（`ctx.commands`） | ✅ `register_command` | 等价 |
| Event | `emit/bail/serial/waterfall` 四种语义 | 21 个事件，链式/短路/原地改参数 | **B 的合并语义不比 A 弱**，`tool_call` 抛异常按 block 处理是个好设计 |
| Service | ✅ `ctx.<name>` + `inject` | ❌ | 两个插件要调同一个内部 API 时只能复制代码 |
| Model Adapter | ✅ `ctx.llm.registerAdapter` | ⚠️ `register_provider` 无消费方 | 声明了但没人用 |
| Client UI | ✅ `dsh.client` Host+Browser | ❌ | 产品决策，暂不做 |

**B 的事件语义其实是被低估的资产**：`input` 的 transform/handled、`tool_result` 的链式替换、`message_end` 的角色不可变约束，比 A 文档里给的四种事件语义更贴近「拦截」场景。别为了对齐 A 把它简化掉。

**补法（2–3 人日）**：加一个**最小服务注册表**——`api.provide(name, obj)` / `api.use(name)`，宿主预置 `http`、`kb`、`cache` 三个服务。不做类型系统、不做 waterfall。

### 2.4 打包、版本与依赖 🔴

**A**：插件是 npm 包，`package.json` 声明 `dsh.bundle.patch`；`dsh plugin --profile X add <spec>` 把参数直接转发给 pnpm（`apps/cli/reference/README.zh.md:85`），因此 npm/git/本地路径都可用；`engines.dsh` 声明兼容范围，安装和启动都会拒绝不满足的组合包（`docs/user/develop/basic/publish.zh.md:66`）；卸载/更新/`why` 都是 pnpm 的。

**B**：一个目录 + `plugin.json`；同一 id 只允许一个版本（`docs/PLUGINS.md`）；没有依赖声明，插件要用 `httpx` 只能假设环境里有；zip 安装是「解压→校验→替换目录」（`app.py:1884-1927`），没有版本目录、没有回滚、没有更新。

**用户可感知后果**：

- 升级 = 卸载再装，出问题无法回滚；
- 插件依赖的库版本和宿主冲突时，整个 worker 起不来；
- 作者无法声明「我只支持 pi ≥ 0.2」，运行时也不检查，坏了才发现。

**补法**：分两步，别一步到位做包管理器。

1. manifest 增加 `requires: {pi, python, packages}`，安装/加载时校验并给出可读错误（2 人日）；
2. 版本目录 `<plugins>/<id>/<version>/` + 原子切换 + 保留上一版（3 人日）；
3. 真正的依赖安装（`uv pip install --target`）放到 P2，只有确实需要时才做。

### 2.5 安装、作用域与启停 🔴

**A**：安装是 **profile 级**的——一个 profile 是一套组合，换 profile 就换一套插件；agent 自己也有 `plugin_manager` 工具可以装/卸/启停。

**B**（产品只做团队形态，单机不在范围内）：

- 插件根是**部署级**的 `data_root/plugins`（`config.py:157`）：装什么由管理员决定，一个包对所有租户可见；
- 安装只有「管理员上传 zip」一条路（`app.py:1870-1877`），作者与管理员之间没有投稿、待审、来源信息；
- 启用是**按用户 + 应用**的（`plugin_enable` 表，`app.py:1976-1994`）——**这一点比 A 更细**：装上不等于所有人可见；
- 同 id 单版本、无更新/回滚；卸载会删目录和**所有租户**的启用记录（`app.py:1931-1946`）。

**用户可感知后果**：团队成员写好插件后只能把 zip 发给管理员，没有「待审列表」；管理员也看不出这个包要联网/读文件/起子进程；灰度只有「启用给哪个应用」这一层；升级没有退路。

**补法（3 人日）**：

- **投稿箱**：作者把 zip 上传到 pending 目录（附 manifest、README、`pikit check` 报告），管理员在插件页审核后安装；
- 审核页直接展示 `permissions` 与静态扫描结果（`pikit check` 已有），而不是让人读代码；
- 版本目录 + 原子切换：`<plugins>/<id>/<version>/`，保留上一版用于回滚（P1）。

### 2.6 分发与市场 🔴

**A**：插件索引是一个 JSON 文件（`format: 1`），可托管在任意静态站点；Discover 页支持多索引、搜索/分类/标签/排序、版本列表与兼容性标记；安装前弹确认（npm 包名 + registry，或 git 仓库 + commit）；仓库还带一个索引生成器 `apps/plugin-market-generator`，从「一行一个包名」生成完整索引。

**B**：`market.py` 里 9 条硬编码条目（4 个 skill + 5 个 plugin，含本次入职的 4 个官方插件），改一条要改代码 + 重启（`README.md:116`）；只能装仓库里自带的包，没有远端源；单文件 skill 的 `scripts/` 不会跟着走；安装会写穿模板软链（`README.md:121-125`）。

**用户可感知后果**：生态不可能长出来——没有第三方可以「发布」一个插件，用户也看不到新东西。

**补法（5 人日）**：

- `/api/market` 支持 `PI_MARKET_URL`（远端 JSON），启动/定时拉取 + 本地缓存 + 失败回退内置目录；
- 条目增加 `version` / `source`（url+sha256）/ `minPiVersion` / `permissions`；
- 安装前展示权限与来源，管理员可禁用远端源；
- 索引格式**直接用 A 的 `format: 1` 兼容子集**——这样将来 DSH 生态的索引也能被 B 读（反向不成立，但成本为零）。

### 2.7 开发体验：这是「轻松」二字的全部 🔴

**A**：`docs/user/develop/` 下有 basic（tool/config/publish）、framework（index/service/events）、practice（llm-adapter/dynamic-cordis）三组教程，仓库整体 2102 篇 `*.zh.md`；`--patch` 可以把本地 TS 文件直接挂进 Web profile 热调；`--dump-config` 看最终组合；tool catalog 由脚本从真实插件生成。

**B（本次补齐前）**：只有 `docs/PLUGINS.md` + `docs/EXTENSIONS.md` + 一个 20 行示例；没有脚手架、没有校验器、没有本地运行器、没有「我的插件加载了吗」的可见性。现在前三个由 `plugin-kit/` 补上，最后一个是 `/api/plugins/status` + 插件页状态行。而**这个唯一的示例曾经是坏的**（2026-11 已修）：

```python
# examples/plugins/hello-pi/main.py:24（修复前）
return ToolResult(text=f"Hello, {name}!")   # TypeError: unexpected keyword argument 'text'
```

`ToolResult` 就是 `pi_agent.AgentToolResult`，字段是 `content/details/usage/terminate/added_tool_names`，没有 `text`。`docs/PLUGINS.md:37` 的代码块同样错误。一个新手照抄，第一次调用工具就炸，且 Web 上只会看到一条工具错误——**一个 20 行的官方示例都能把错误带到线上，说明缺的不是文档，是校验**。

**一个 Python 开发者从零到「模型调用成功」现在要跨过**：manifest 字段与 entry 规则、`setup` 的三种别名、工具名全局唯一、装饰器的四种参数形态、worker 重启才生效、错误只在服务端 stderr。**预计 2–4 小时，失败率高**。

**已经补的**：`plugin-kit/`（`new/check/dev/pack`）、`/api/plugins/status`、插件页状态行、`api.config`、
per-tool 超时、投稿箱。**还差的**：`requires` 运行时校验、版本目录与回滚、L1 声明式工具。

### 2.8 安全与治理 🟠

**A 的模型**：插件代码本身**不隔离**（和 B 一样），官方 SAFETY.md 明说未经安全审计；但工具调用有 `ctx.approval`（fail-closed 四值）、权限预设、文件效果策略、bash 走沙箱。安装有来源确认、registry 校验、`engines` 兼容检查。

**B 的模型**：插件与宿主同进程、同权限；`permissions` 只是声明，v1 不强制执行（`docs/PLUGINS.md:32`）；唯一门控是「管理员才能装」。

**B 特有的风险（多租户）**：

1. 插件被当作 **builtin** 路径加载（`bootstrap.py:341`），所以 `no_extensions=True`（`app.py:649`）也拦不住它——**一个管理员装的插件可以读 `data_root/agents/*/sessions/*.jsonl` 里所有租户的对话**；
2. 插件里的死循环/阻塞调用会拖死整个 worker，没有 per-tool 超时；
3. 用户启用插件时看不到「它会联网/会读文件/会起子进程」，没有知情同意。

**补法**：per-tool 超时 + AbortSignal 贯通（2 人日）；启用确认页展示 `permissions` 并从代码静态推断「声称 vs 实际」（1 人日，`pikit check` 已实现原型）；子进程隔离放 P2。

> 别把这条写成「对齐 A」。A 也没解决插件可信问题。B 要解决的是**多租户下的爆炸半径**，这比 A 的场景更具体。

### 2.9 可观测与自举 🟠

**A**：`plugin_manager` / `plugin_market` 是模型可调用的工具——agent 能自己装插件；有 telemetry、诊断包。

**B**：`load_extensions` 的 `ExtensionDiagnostic` 通过 `on_warning` 打到 stderr（`bootstrap.py:329-333`）；Web 上只有一个「启用/停用」开关，看不到「加载成功了吗、注册了几个工具、上次报什么错」。

**补法（2 人日）**：worker 启动时把 diagnostics + 已注册工具/命令存进 registry；`GET /api/plugins/{id}/status` 返回；插件页每个条目显示「已加载 3 个工具 / 1 个错误」并可展开。

---

## 3. 三个「看起来是差距、其实不该抄」

| A 的东西 | 为什么不抄 | 替代 |
|---|---|---|
| Cordis 全家桶（依赖注入、Fiber、类型级 waterfall、HMR） | 实现成本以月计，B 的插件数量撑不起这套复杂度；HMR 对 Python 尤其难做对 | 200 行的 registry + `effect` + `requires` |
| npm/pnpm 安装器、SemVer 豁免、build 脚本审批 | Python 侧 `uv` 已经解决依赖；B 缺的是**清单**和**白名单**，不是安装器 | manifest `requires` + 预检 |
| 71 个工具、59 个 `ui-*` 包 | 先把横切能力（spill/超时/策略/渲染）做出来，工具是长尾 | 按用户需求加，不按 A 的目录加 |

同样，**不要为了「像 A」而把 `setup(api)` 改成 class + DI**。B 的简单是优势，补平台能力不等于加抽象层。

---

## 4. 真正的目标：三级扩展阶梯 + 一条 AI 生产线

B 的用户不是 TypeScript 工程师。对他们来说，「写 Python 插件」本身就是最后手段，不是入口。

### 4.1 三级阶梯

| 层级 | 作者 | 写什么 | 覆盖的需求 | 现在 | 成本 |
|---|---|---|---|---|---|
| **L0 Skill** | 任何人 | `SKILL.md`（提示词+流程+格式） | 判断标准、输出格式、话术、SOP | ✅ 已成熟 | 0 |
| **L1 声明式工具** | 业务/运营 | 一份 YAML/JSON：URL、参数映射、响应裁剪 | 调内部 API、查库、读固定目录 | ❌ 没有 | 5–7 人日 |
| **L2 Python 插件** | 有 Python 基础 | `setup(api)` + 工具函数 | 复杂逻辑、状态、多步、外部 SDK | ⚠️ 裸 SDK | 见 §5 |
| **L3 UI 插件** | 前端 | React slot | 面板、卡片、专用视图 | ❌ | 2–3 周 |

**为什么 L1 是战略级的**：

- 大多数「我需要一个插件」的真实需求是「调这个接口，把结果整理成模型能用的样子」——不需要图灵完备；
- L1 **同时解决易用性和安全性**：没有任意代码执行，权限可以精确到「域名白名单 + 方法 + 超时 + 响应字段」，天然可审计、可限流、可 mock；
- A 没有这一层（它假设你会写 TS）。这是 B 可以**领先**而不是追赶的地方；
- 实现上不需要新语言：一份 `plugin.yaml`，服务端解析成 `AgentTool` 直接注册，不进 Python 导入器。

建议的 L1 形态（先做只读）：

```yaml
# my-plugin/plugin.yaml
id: kb-search
name: 知识库检索
tools:
  - name: kb_search
    description: 检索内部知识库，返回标题+摘要+链接。需要查资料时调用。
    request:
      method: GET
      url: https://kb.internal/api/search
      query: { q: "{query}", top_k: "{top_k}" }
      headers: { Authorization: "Bearer ${KB_TOKEN}" }
      timeout: 15
      allow_hosts: [kb.internal]        # 强制白名单
    parameters:
      query: { type: string, required: true, description: 检索词，≤200 字 }
      top_k: { type: integer, default: 5, minimum: 1, maximum: 20 }
    response:
      items: /data/items
      text: |
        {{#each items}}- {{title}}（{{source}}）{{url}}
        {{/each}}
      max_chars: 6000
```

### 4.2 AI 生产线：让产品自己生产插件

这是「轻松」的真正解锁点，而且 B 已经具备全部零件（agent + 文件工具 + skill + 插件加载器）：

```
用户说需求
  → agent 用「插件工坊」skill 访谈（5 问：何时用/输入/输出/调什么/失败怎么办）
  → 判定层级：流程类 → 写 SKILL.md；接口类 → 写 L1 yaml；复杂类 → 写 L2 main.py
  → 在 workspace 里生成目录
  → 确定性校验（pikit check：manifest/签名/工具 schema/权限一致性）
  → 试跑（pikit dev：给定样例参数，必须真的返回）
  → 打包 zip，告诉用户在「插件」页上传 / 或调安装接口
  → 用户启用 → 下一条消息模型就能看到工具
```

**硬规则**：agent 只负责**写**，验收必须由确定性工具做（`check` + `dev` + 一次真实调用）。否则「AI 写的插件」会变成新的故障源——`hello-pi` 的 `ToolResult(text=...)` 就是活例子。

本次交付里已经放了这条生产线的三个端点：`template/skills/plugin-creator/SKILL.md`（访谈与生成规范）、`plugin-kit/`（校验与试跑）、`plugins/`（四个可以直接用的样板）。

---

## 5. 路线图

工时按「熟悉本仓库的一名 Python 工程师」估算，含测试。P0 的目标不是「追平 A」，而是**让团队成员 10 分钟写/生成一个插件，管理员一次审核后就能在应用里用上**。

### P0：自服务闭环（1–2 周）

| # | 事项 | 改动点 | 工时 | 验收 |
|---|---|---|---|---|
| 0 | 修 `hello-pi` 与文档的错误写法（**已完成**） | `examples/plugins/hello-pi/main.py`、`docs/PLUGINS.md` | 0.2d | 示例工具真实调用返回文本 |
| 1 | 插件开发工具 `plugin-kit`（**已完成**） | 新增 `plugin-kit/` | 2d | `new/check/dev/pack` 四个命令在模板上跑通 |
| 2 | 官方插件包 + 市场条目（**已完成**） | 新增 `plugins/`、`market.py` 4 条 | 2d | 4 个插件通过 `check/dev/pack` 与 pytest；市场可一键安装 |
| 3 | 加载状态可见（**已完成**） | `ExtensionRunner` 保留 diagnostics、`GET /api/plugins/status`、`PluginsView` 展示 | 2d | 插件坏掉时 UI 显示原因与已注册工具 |
| 4 | `api.config` 注入（**已完成**） | loader 读 `plugin.json`：默认值 + `env` 覆盖 | 0.5d | 同一插件两套配置不改代码 |
| 5 | 文档与技能 | `docs/PLUGIN-DEV.md`、`plugin-creator` skill、README 入口 | 1.5d | 新人按文档 10 分钟跑通首个工具 |

### P1：可治理、可分发（3–4 周）

| # | 事项 | 工时 | 验收 |
|---|---|---|---|
| 7 | manifest v2 运行时校验（`requires`/`limits`/`permissions`/`config`） | 2d | 不满足 `requires.pi` 拒绝加载并说明原因 |
| 8 | 生命周期：`api.effect()` + 逆序 dispose | 2d | 反复启停不累积监听器/连接 |
| 9 | per-tool 超时（**已完成，最小版**：`limits.timeoutSeconds` → `AgentTool.timeout_seconds`；取消 await，不杀线程） | 2d | 死循环插件只失败一次调用，不拖死 worker |
| 10 | 最小服务缝：`api.provide/use` + 内置 `http`/`kb`/`cache` | 3d | 两个插件共用同一客户端，不复制代码 |
| 11 | 投稿箱与审核（**已完成**）：作者提交、管理员在「待审」页安装/驳回 | 2d | 作者→上线 ≤ 1 个工作日，审核不看代码 |
| 12 | 远端插件索引（兼容 A 的 `format: 1` 子集） | 5d | 换 `PI_MARKET_URL` 即可看到新插件，无需发版 |
| 13 | L1 声明式工具（只读 HTTP 先行） | 5–7d | 业务同学用 yaml 做出一个可用工具，零 Python |
| 14 | 版本目录 + 原子切换 + 回滚 | 3d | 升级失败自动回退上一版 |

### P2：按需

UI slot（1 个，先做「会话内卡片」）· 子进程/容器隔离插件执行 · 签名与审核流 · 评分/安装量 · HMR · 插件依赖安装（`uv --target`）。

### 与 A 的对齐顺序（如果一定要对齐）

`开发工具链` → `分发/索引` → `生命周期` → `服务缝` → `UI`。**不要**从 Cordis 或 71 个工具开始。

---

## 6. 怎么证明「用户能自己做了」

| 指标 | 现状（估） | P0 目标 | 测量方式 |
|---|---|---|---|
| 首个工具被模型调用成功的中位时长 | 2–4 小时 | ≤ 10 分钟 | 新人任务计时（3 人样本） |
| 首次 `check` 通过率 | — | ≥ 80% | pikit 埋点/日志 |
| 插件加载失败可诊断率 | 0（只有 stderr） | 100% | UI 上能看到原因的比例 |
| 非管理员安装占比 | 0 | 不适用 | 团队形态只允许管理员安装 |
| 作者投稿 → 上线中位时长 | 无通道 | ≤ 1 个工作日 | 投稿箱时间戳 |
| 插件被 ≥2 个应用启用 | 未统计 | ≥ 50% | `plugin_enable` 聚合 |
| 插件导致的 worker 异常重启 | 未统计 | < 1 次/周 | registry 事件 |
| 零 Python 插件占比（L0+L1） | 0 | ≥ 40% | manifest/类型统计 |

---

## 附录 A：本文引用的事实位置

| 事实 | 位置 |
|---|---|
| 默认激活 5 个工具、内置 8 个 | `packages/pi-coding-agent/src/pi_coding_agent/tools/__init__.py:38-41` |
| 插件被当作 builtin 加载 | `packages/pi-coding-agent/src/pi_coding_agent/bootstrap.py:341` |
| 团队模式禁用户扩展 | `packages/pi-server/src/pi_server/app.py:649` |
| 插件路径来自 DB 启用集 | `packages/pi-server/src/pi_server/app.py:1165-1169` |
| worker 驱逐 = 重载 | `packages/pi-server/src/pi_server/app.py:787-799` |
| 安装：团队模式 + 管理员 | `packages/pi-server/src/pi_server/app.py:1870-1877` |
| 启停按用户/应用 | `packages/pi-server/src/pi_server/app.py:1976-1994` |
| manifest 扫描规则 | `packages/pi-server/src/pi_server/plugins.py:45-90` |
| 市场目录硬编码 | `packages/pi-server/src/pi_server/market.py:40-158` |
| `setup(api)` 只传一个参数 | `packages/pi-coding-agent/src/pi_coding_agent/extensions/loader.py:335-337` |
| `register_provider` 无消费方 | `packages/pi-coding-agent/src/pi_coding_agent/extensions/api.py:238` |
| 21 个事件 | `packages/pi-coding-agent/src/pi_coding_agent/extensions/types.py:35-59` |
| 示例的 `ToolResult(text=...)` | `examples/plugins/hello-pi/main.py:24` |
| 文档同错 | `docs/PLUGINS.md:37` |
| DSH `plugin add` 直通 pnpm | `apps/cli/reference/README.zh.md:85` |
| DSH `engines.dsh` | `docs/user/develop/basic/publish.zh.md:66` |
| DSH 插件索引格式 | `docs/user/guide/plugin-market.md` |
| DSH Fiber/effect | `docs/user/develop/framework/index.zh.md` |

## 附录 B：顺手要修的过期文档

| 位置 | 现状 | 应为 |
|---|---|---|
| `docs/PLUGINS.md:60-67` | 「没有上传/审核/签名流程」「安装 = 放目录」 | zip 上传、市场安装、卸载、worker 驱逐都已实现 |
| `docs/PLUGINS.md:32` | `permissions` 不强制执行 | 仍成立，但要写明「因此启用即等于授权全部权限」 |
| `docs/GAP-ANALYSIS-vs-DSH.md:48` | 「7 个工具，默认 4 个」 | 现在 8 个，默认 5 个（含 `present`） |
| `README.md:116` | 市场改完要重启 | 加入「P1 远端索引」后不再成立 |
| `docs/DEVIATIONS.md` | 无插件安全条目 | 登记「插件与宿主同权限」「builtin 绕过 no_extensions」 |

## 附录 C：本次交付

| 文件 | 作用 |
|---|---|
| `plugin-kit/` | `pikit new/check/dev/pack`：脚手架、校验、本地试跑、打包 |
| `plugin-kit/templates/basic` | 最小工具插件模板（含 `api.config` 的正确写法） |
| `plugin-kit/templates/http` | HTTP 工具模板（超时、错误、响应裁剪、`asyncio.to_thread`） |
| `template/skills/plugin-creator/SKILL.md` | 让 agent 按访谈生成插件、自检、打包的规范 |
| `plugins/clinical-calc` | BMI/BSA、eGFR/CG、CHA₂DS₂-VASc/HAS-BLED、Child-Pugh/MELD-Na |
| `plugins/pubmed` | PubMed 检索与摘要（E-utilities，内置限流与字段裁剪） |
| `plugins/web-fetch` | 网页抓取：SSRF 防护、大小/超时上限、HTML 去噪、JSON 格式化 |
| `plugins/csv-tools` | 工作区 CSV 探查与条件查询（走 `workspace_path`） |
| `plugins/build.sh` | 校验并打包四个插件到 `dist/` |
| `packages/pi-server/src/pi_server/market.py` | 新增 4 条官方插件条目，市场可一键安装 |
| `extensions/api.py` + `extensions/loader.py` + `bootstrap.py` | 插件现在拿得到 `api.cwd`（工作区），不再依赖进程 cwd |
| `packages/pi-coding-agent/.../plugins/__init__.py` | 新增 `workspace_path()`：把相对路径安全地解析到工作区 |
| `packages/pi-server/.../app.py`（状态/投稿） | `GET /api/plugins/status`、投稿箱四个端点；顺带修了「安装插件」用 PUT 打 POST 路由的 405 |
| `extensions/runner.py` + `agent_session.py` | 保留加载诊断并暴露 `status()`，UI 才能解释「为什么没生效」 |
| `pi-agent-core`（`types.py` + `loop.py`） | `AgentTool.timeout_seconds` + agent 循环里的 `wait_for` |
| `web/src/workbench/Workbench.tsx` | 插件状态行、待审页、提交入口；`api.postRaw` |
| `examples/plugins/hello-pi/main.py` | 修正为可直接运行的写法 |
| `docs/PLUGINS.md` | 修正示例，补 config/limits/状态/投稿说明 |
