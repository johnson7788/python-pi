# pi 周边/host 包与整体架构研究报告

> 研究对象：`/tmp/pi-ref`（Earendil Works 的 "pi" coding agent），package version `0.85.1`，HEAD `f3c6722`（2026-09-11）。
> 覆盖包：`tui`、`chord`、`protocol`、`client`、`server`、`session-backends/sqlite-node`、`telemetry`、`evals`，以及根目录 `README.md`、`AGENTS.md`、`package.json`、`scripts/`、`tui-plan.md`。
> 目的：为一个忠实的 Python 移植判断"哪些包必须移植、哪些可以推迟或不做"。
> 统计口径：`find <pkg>/src -name '*.ts' | xargs wc -l`，只算 `.ts`，不含 `test/`，不含生成文件。行数均为实测。

---

## 0. 结论速览（给移植决策者的 12 条）

1. 这是一个 **11 个 workspace 包** 的 npm monorepo，核心三件套是 `ai`（多 provider LLM）、`agent`（agent 运行时）、`coding-agent`（CLI）；`tui` 是它们唯一依赖的 UI 库。
2. 真正的复杂度集中在 `coding-agent`（**70,052 LOC**）、`agent`（**25,305 LOC**）、`ai`（**24,384 LOC**）、`tui`（**18,107 LOC**）。
3. **`tui` 是从零手写的渲染器**，不是 ink/React 封装。运行时依赖只有 `get-east-asian-width`（东亚字符宽度）和 `marked`（Markdown 词法）。这是移植工作量最大、也最"值钱"的周边包。
4. `tui` 有两种渲染器：`TuiMainScreen`（主屏 + 终端 scrollback，行级 diff）与 `TuiAltScreen`（备用屏 + 应用自管滚动 + 固定高度视口 + 布局树），两者共享 `TUI` 接口与同一批组件实例。
5. `tui` 的组件模型极简：`Component { render(width): string[]; handleInput?; handleMouse?; invalidate? }`。布局用 `VStack`/`HStack`/`ScrollView` 的手写 flex 子集（`basis/grow/shrink/minSize/maxSize/visible`），不是 CSS flexbox。
6. `chord`（`@earendil-works/chord`，**5,822 LOC**）是独立的"应用组合运行时"（facets/services/replicated state/delta/RPC）。**coding-agent 只在 `src/experimental/**` 里用它**；但 `agent` 运行时确实硬依赖它的 `./context` 子路径（121 LOC 的取消/上下文原语）。
7. `protocol`+`client`+`server`（合计 **3,970 LOC**）是 **CBOR 二进制远程协议栈**，与 `pi --mode rpc` 的 stdin/stdout JSONL 模式是**两套完全不同的东西**。它们是 coding-agent 的 `devDependencies`，属于实验特性。
8. `session-backends/sqlite-node`（**1,973 LOC TS + 121 LOC SQL**）是 JSONL session store 的替代实现，**没有任何包依赖它**，纯可选。
9. `telemetry`（**935 LOC**）是 vendor-neutral 的埋点契约，消费方永远有 no-op 兜底，非必需。
10. `evals`（**1,351 LOC**，private）是跑真实模型的开发期评测，不进任何构建/运行时依赖图，可完全不移植。
11. 仓库的 build 链是显式的拓扑序：`chord → tui → telemetry → ai → agent → sqlite-node → protocol → client → server → coding-agent`（见根 `package.json` 的 `scripts.build`）。这个顺序本身就是依赖分层图。
12. Python 版 v0.1 建议：`utilities → ai → agent(+JSONL session) → tui → coding-agent(tools/CLI/interactive/JSONL RPC)`；**chord 只做 `./context` 的等价物**；protocol/client/server、facets、evals 全部推迟；sqlite 与 telemetry 视需要可选。

---

## 1. 仓库全景

### 1.1 包清单与体量

| 目录 | npm 包名 | src LOC | src 文件数 | test LOC | 定位 |
|---|---|---|---|---|---|
| `packages/ai` | `@earendil-works/pi-ai` | 24,384 | 179 | 37,437 | 多 provider LLM 统一 API |
| `packages/agent` | `@earendil-works/pi-agent-core` | 25,305 | 92 | 21,238 | agent 运行时（loop、工具、session、harness） |
| `packages/coding-agent` | `@earendil-works/pi-coding-agent` | 70,052 | 258 | 57,036 | 交互式 coding agent CLI |
| `packages/tui` | `@earendil-works/pi-tui` | 18,107 | 42 | 18,617 | 差分渲染终端 UI 库 |
| `packages/chord` | `@earendil-works/chord` | 5,822 | 24 | 3,553 | 应用组合运行时（services/facets/state/RPC） |
| `packages/session-backends/sqlite-node` | `@earendil-works/pi-session-backend-sqlite-node` | 1,973 (+121 SQL) | 15 | 1,909 | SQLite session 后端 |
| `packages/server` | `@earendil-works/pi-server` | 1,966 | 16 | 1,057 | 实验性本地 server |
| `packages/evals` | `@earendil-works/pi-evals`（private） | 1,351 | 9 | 463 | 模型驱动的行为评测 |
| `packages/client` | `@earendil-works/pi-client` | 1,135 | 8 | 799 | 远程 session 客户端 |
| `packages/telemetry` | `@earendil-works/pi-telemetry` | 935 | 6 | 243 | 埋点契约 + 参考适配器 |
| `packages/protocol` | `@earendil-works/pi-protocol` | 869 | 8 | 567 | CBOR wire protocol |

周边包（本报告重点）合计约 **32,000 LOC src**，其中 `tui` 一家占 **56%**。

### 1.2 根目录文件

- **`README.md`（115 行）**：项目定位（"Pi agent harness"+ self-extensible coding agent），三个主力包的 npm 名，明确写 "Pi does not include a built-in permission system"，沙箱化交给扩展（Gondolin / Docker / OpenShell）。有 supply-chain hardening 章节（直接依赖 pin 死版本、`npm-shrinkwrap.json`、生命周期脚本 allowlist）。
- **`AGENTS.md`（124 行）**：给人和 agent 的开发规则。与移植相关的硬约束：
  - 只允许 **erasable TypeScript syntax**（Node strip-only mode）：不能用 parameter properties、`enum`、`namespace`、`import =`。这解释了为什么 `tui-plan.md` 反复强调"用显式字段+构造函数赋值"。
  - 禁止 inline import（`await import()`）。
  - 禁止硬编码按键判断，必须进 `DEFAULT_EDITOR_KEYBINDINGS` / `DEFAULT_APP_KEYBINDINGS`。
  - `never run npm run build / npm test unless requested`；测试用 `./test.sh`。
- **`package.json`**：`"type": "module"`，`workspaces: packages/*` + `packages/session-backends/*` + 5 个 coding-agent examples。`scripts.build` 把包的构建顺序显式写死（拓扑序）。`engines.node >=22.19.0`。
- **`scripts/`（41 个文件）**：工程化脚本为主。与移植理解相关的：
  - `build-binaries.sh`（6.2 KB）：产出 `pi-darwin-arm64` / `pi-linux-x64` / `pi-windows-x64` 等 6 平台独立二进制；带 `--offline-model-data`（用打包好的模型数据，不联网刷新）。
  - `check-entry-graphs.mjs`：把每个 `exports` 入口的 **value import 图** 当作成本契约来校验（注释说明"一个 `export *` 会让 1 文件的纯函数入口拉进约 37 MB 模块图"）。这是 pi 分层的工程化保障。
  - `check-pinned-deps.mjs` / `check-runtime-deps.mjs` / `generate-coding-agent-shrinkwrap.mjs` / `check-ts-relative-imports.mjs`。
  - `profile-coding-agent-node.mjs`（20 KB）：TUI/RPC 两种模式的 profiling。
  - `session-context-stats.mjs`、`edit-tool-stats.mjs`、`read-tool-stats.mjs`：从 session transcript 里统计 token/工具使用。
- **`tsconfig.base.json`**：`target ES2022`、`module Node16`、`strict`、`erasableSyntaxOnly: true`。
- **`test.sh` / `mini-test.sh`**：前者在临时 HOME 里隔离跑全量非 e2e 测试；后者专用于 `experimental/mini` 的 TUI + 分离式 session server。
- **`.pi/`**：仓库自身的 pi 资源（4 个 extensions、6 个 prompts、3 个 skills、npm/git 缓存目录）——即"用 pi 开发 pi"。
- **`tui-plan.md`（1,001 行）**：见 §2.10。

---

## 2. `tui` — `@earendil-works/pi-tui`（重点）

### 2.1 定位与包信息

`packages/tui/package.json`：version `0.85.1`，描述 "Terminal User Interface library with differential rendering for efficient text-based applications"。
**运行时依赖只有两个**：`get-east-asian-width@1.6.0`（CJK/emoji 宽度）、`marked@18.0.11`（Markdown tokenizer）。devDeps：`@xterm/headless`（测试用虚拟终端）、`chalk`。
**没有任何包依赖它，除了 `coding-agent`**（运行时依赖）——即它是为 coding-agent 量身定做、但按独立库发布的。

**它是从零写的，不是 ink 封装。** 没有 React、没有 `yoga`、没有 `blessed`/`neo-blessed`。渲染、diff、按键解析、ANSI 宽度、grapheme 切分、Kitty/iTerm2 图像、鼠标 SGR 解析全部自己实现。

### 2.2 公共 API 面（`src/index.ts`，156 行）

导出可分为 8 组：

- **渲染器/接口**：`TUI`、`ViewportTUI`+`isViewportTUI`、`TuiBase`（内部基类）、`TuiMainScreen`、`TuiAltScreen`（+`TuiAltScreenOptions`、`TuiMainScreenRenderState`）、`TuiMode`、`ProcessTerminal`、`Terminal`。
- **核心类型**：`Component`、`Container`、`Focusable`+`isFocusable`、`CURSOR_MARKER`、`TuiMouseEvent`/`TuiMouseEventResult`/`TuiMouseButton`/`TuiMouseEventType`、`TuiInputListener`、`Overlay*`、`SizeValue`、`compositeTuiLine`。
- **组件**：`Box`、`VStack`/`HStack`（+`StackChild`/`StackEntry`/`StackEntryOptions`/`StackOptions`）、`Spacer`、`Text`、`TruncatedText`、`Input`、`Editor`、`Markdown`、`Loader`、`CancellableLoader`、`SelectList`、`SettingsList`、`ScrollView`、`Image`、`MouseRegion`。
- **按键**：`Key`、`KeyId`、`matchesKey`、`parseKey`、`isKeyRelease`/`isKeyRepeat`、`decodeKittyPrintable`、`isKittyProtocolActive`/`setKittyProtocolActive`。
- **keybindings**：`TUI_KEYBINDINGS`、`KeybindingsManager`、`getKeybindings`/`setKeybindings`、`Keybinding*` 类型。
- **终端能力/图像**：`detectCapabilities`/`getCapabilities`/`setCapabilities`/`setCapabilityOverrides`、`renderImage`、`encodeKitty`/`encodeITerm2`、`hyperlink`、`getPngDimensions`/`getJpegDimensions`/`getGifDimensions`/`getWebpDimensions`、`calculateImageRows`、`imageFallback`、`getCellDimensions`、Kitty 图像删除 API。
- **工具**：`visibleWidth`、`truncateToWidth`、`sliceByColumn`、`stripTerminalSequences`、`wrapTextWithAnsi`、`getOsc8LinkAtColumn`。
- **其他**：`Marked`/`Token`（转出 marked 类型）、`Autocomplete*`、`CombinedAutocompleteProvider`、`fuzzyMatch`/`fuzzyFilter`、`renderLatex`、`StdinBuffer`、`getNativeClipboard`、`parseOsc11BackgroundColor`、`parseTerminalColorSchemeReport`、`EditorComponent`。

### 2.3 渲染循环（render loop）

`TuiBase`（`src/tui.ts:465` 起，1,456 行的核心）统一管理生命周期、焦点、overlay、输入分发和渲染节流：

- `requestRender(force?)`（`tui.ts:952`）：置位 `renderRequested`，然后 `process.nextTick(scheduleRender)`。
- `scheduleRender()`（`tui.ts:985`）：以 `MIN_RENDER_INTERVAL_MS = 16`（≈60fps）做节流，`setTimeout` 到点后调 `doRender()`；若渲染期间又置位则递归续排。
- `requestImmediateRender()`（`tui.ts:961`）：**用户输入抢占节流帧**——取消 timer、nextTick 立即渲染。注释明确写了这是为了让按键响应不被 throttled frame 推迟。
- `renderNow(force)`：同步渲染。
- 术语上这是"**按需渲染 + 帧率上限 + 输入优先**"，没有独立 layout/render 循环；`tui-plan.md` 第 388 行把这条写成规则："**No render means no layout**"。

`TuiMainScreen.doRender()`（`src/tui-main-screen.ts:247`，655 行）的三种策略（README "Rendering modes"）：

1. **首次渲染**：直接输出所有行，不清屏、不破坏 scrollback。
2. **宽度变化 / 视口上方内容变化**：清屏 + 全量重渲。
3. **普通更新**：定位到第一处变化的行，清到末尾，只写变化行。

它维护 `previousLines`、`hardwareCursorRow`、`previousViewportTop`、`maxLinesRendered`，并处理 **Kitty 图像占用多行** 的特殊重绘（`imageReservedRows`，先下移再上移回来写图）。内容缩短时默认 **不清空旧行**（`clearOnShrink` 默认 false）以省重绘。

`TuiAltScreen.doRender()`（`src/tui-alt-screen.ts:1652`，1,721 行）完全不同的模型：

```
root = layoutRoot ?? implicitScrollView
nextLayout = renderLayoutFrame(root, width, height, requestRender)
screen = 后处理(search highlight → scroll-to-end indicator → overlays → 选择高亮 → flash)
cursorPos = extractCursorPosition(screen)
screen = applyLineResets(screen) + 宽度裁剪
fullRedraw = 尺寸变化或首帧；否则逐行比较 previousScreen
对每行: 若变化 → "\x1b[{row+1};1H\x1b[2K" + 行内容
```

**两端都使用 synchronized output**（`\x1b[?2026h` … `\x1b[?2026l`）包裹整帧写入，实现原子/无闪烁更新。`TuiMainScreen` 还实现了 `BoundedTerminalWriter`，在写大帧时分块并规避 UTF-16 surrogate 截断（`tui-main-screen.ts:15`）。

### 2.4 组件模型

接口极小（`src/tui.ts:111`）：

```ts
interface Component {
  render(width: number): string[];     // 每行必须 <= width
  handleInput?(data: string): void;    // 焦点组件收到原始终端字串
  handleMouse?(event: TuiMouseEvent): TuiMouseEventResult | undefined;
  invalidate?(): void;                 // 清缓存，下次 render 重算
}
```

约定：TUI 在每行末尾追加完整 SGR reset + OSC 8 reset，**样式不跨行**；组件要么逐行重加样式，要么用 `wrapTextWithAnsi()`。IME 支持靠 `Focusable` + `CURSOR_MARKER`（`\x1b_pi:c\x07`，一个零宽 APC 序列）：聚焦组件在光标处插 marker，TUI 扫描最终合成行、定位硬件光标（默认隐藏但定位，满足 IME 候选窗定位）。

组件清单一览（含 LOC）：

| 组件 | 文件 | LOC | 要点 |
|---|---|---|---|
| `Container` | `tui.ts`（内） | — | 子组件顺序渲染、鼠标几何路由、overlay buffer |
| `Box` | `components/box.ts` | 167 | padding + 背景函数，按 width/bg/child 输出缓存 |
| `Text` | `components/text.ts` | 107 | 自动换行（`wrapTextWithAnsi`），缓存 text/width/lines |
| `TruncatedText` | `components/truncated-text.ts` | 65 | 单行截断，不换行 |
| `Spacer` | `components/spacer.ts` | 28 | N 个空行 |
| `Stack`/`VStack`/`HStack` | `components/stack.ts` 154 / `v-stack.ts` 33 / `h-stack.ts` 44 | 231 | 手写 flex 子集，轴无关分配器 `allocateStackSizes` |
| `ScrollView` | `components/scroll-view.ts` | 224 | follow-end、overscroll chain/contain、滚动条状态、hit test |
| `Input` | `components/input.ts` | 494 | 单行输入、kill-ring、undo、bracketed paste、grapheme 光标 |
| `Editor` | `components/editor.ts` | **2,461** | 多行编辑器：完整 Emacs keymap、undo、kill ring、历史、autocomplete、paste marker、鼠标选择 |
| `Markdown` | `components/markdown.ts` | **1,015** | 基于 `marked` 的 token → ANSI 渲染、LaTeX、图片行、缓存 |
| `SelectList` | `components/select-list.ts` | 273 | 过滤列表 + 分栏 label/description + 鼠标 |
| `SettingsList` | `components/settings-list.ts` | 328 | 设置项、值循环、子菜单、搜索 |
| `Loader`/`CancellableLoader` | `components/loader.ts` 101 / `cancellable-loader.ts` 40 | 141 | 动画 spinner |
| `Image` | `components/image.ts` | 127 | Kitty/iTerm2 内联图 + 文本 fallback |
| `MouseRegion` | `components/mouse-region.ts` | 33 | 给任意组件加鼠标行为而不改渲染 |
| `alt-screen-flash` | `components/alt-screen-flash.ts` | 51 | 备用屏闪烁提示 |

`Markdown` 还带自定义 keybinding/主题 hook，并对 `~~del~~` 做了更严格的 tokenizer 覆盖（`STRICT_STRIKETHROUGH_REGEX`）；LaTeX 由独立的 `latex.ts`（**1,394 LOC**）渲染成终端字符。

### 2.5 布局（layout）

**两套布局语义，刻意的**（`tui-plan.md` 第 26–70 行）：

- `TuiMainScreen`：终端拥有滚动，应用**不假装**能做 sticky 行、嵌套滚动、全高并排 pane、可靠鼠标 hit test。交互模式就是一份"竖直文档"：header → loaded resources → chat → pending → status → widgets → editor → widgets → footer。
- `TuiAltScreen`：应用拥有固定高度视口，组成 **可滚动 transcript + 固定底部 dock**。

公开布局原语只有 4 个：`VStack`、`HStack`、`ScrollView`、既有 overlays（`tui-plan.md` 核心决策 4）。stack entry 选项（`components/stack.ts`）：

```ts
interface StackEntryOptions {
  basis?: number | "auto";   // 主轴初始尺寸
  grow?: number;             // 正剩余空间份额
  shrink?: number;           // 溢出时收缩意愿
  minSize?: number; maxSize?: number;
  visible?: (viewport: LayoutViewport) => boolean;  // 响应式
}
interface StackOptions { gap?: number; align?: "stretch"|"start"|"center"|"end"; }
```

**内部布局树是每帧重建的临时快照**，与长期存活的组件树分离：`layoutComponent()` 递归计算 `LayoutBox { component, rect, clip, children, parent, lines, lineOffset, scrollView, layer }`，`paintBox()` 把叶子行按 rect+clip 合成到 `screen: string[]`（`src/layout.ts`，449 行）。关键工程取舍：

- 叶子 `render(width)` 结果**按引用**存进 `LayoutBox.lines`，复用组件自己的缓存（Markdown/Text/Image/Box 都有）；`layout.ts` 内另有一层 `renderCache: Map<Component, Map<width, lines>>`。
- 热路径：全宽 box 且目标行为空时直接引用源行字符串，避免每帧过 ANSI/grapheme 重分段（`layout.ts:345` 注释）。
- 水平合成用 `compositeTuiLine()`，禁止用 `String.length` 或裸 `substring` 做终端列运算。
- overlay 不在第一版布局树里，而是布局完成后按屏幕坐标合成。
- `ScrollView` 在布局中把子内容按自然高度渲染、按 `scrollTop` 平移、累积 clip，只画与视口相交的行。
- 键盘滚动 action 进 `TUI_KEYBINDINGS`（`tui.altScreen.pageUp/pageDown/top/bottom/halfPageUp/.../previousPrompt/nextPrompt`），永不硬编码。
- OSC 133 语义标记（`\x1b]133;A`）用于 prompt 间跳转，在 layout/paint 时被剥掉。

`tui-plan.md` 的非目标明确排除：CSS flexbox、grid、wrap flex、任意绝对定位、百分比尺寸、虚拟化 transcript、增量布局树变更、给自定义组件公开内部 layout node。测试计划覆盖 stack 分配（auto/numeric basis/grow/shrink/min-max clamp/奇数格取整/可见性/gap/ANSI 与超链接不泄漏/CJK+emoji+组合字符边界）、ScrollView 状态机、layout frame 几何/clip/hit test、备用屏行为、图像不回归。

### 2.6 键盘与按键

- `keys.ts`（**1,401 LOC**）实现 **Kitty keyboard protocol** 与 legacy 序列双支持：`matchesKey(data, keyId)`、`parseKey`、`Key.*` 类型化键 id、`isKeyRelease`/`isKeyRepeat`、`decodeKittyPrintable`。文件头显式注明参考了 kitty 协议文档与 opentui 的 parse.keypress。
- `ProcessTerminal`（`terminal.ts:1`，547 行）负责协议协商：发送 `\x1b[>7u\x1b[?u\x1b[c`（请求 Kitty flags + DA1），解析响应（`parseKeyboardProtocolNegotiationSequence`），设 `setKittyProtocolActive`。还有 Apple Terminal Shift+Enter 特例、SIGWINCH 尺寸刷新（对 `kill(2)` 被 LSM 拒绝的 EACCES 做 best-effort 兜底）、`drainInput()`（防止 Kitty key-release 在慢 SSH 上泄漏到父 shell）。
- `keybindings.ts`（320 行）：`TUI_KEYBINDINGS` 是一张大表（`tui.editor.*`、`tui.input.*`、`tui.select.*`、`tui.altScreen.*`…），每项 `{ defaultKeys, description }`；`KeybindingsManager` 支持用户覆盖；`getKeybindings()` 取全局单例。策略是"**所有按键都必须是可配置的顶级 action**"。
- `stdin-buffer.ts`（444 行）：因为 stdin 的 data 事件会**拆散转义序列**（例如鼠标序列 `\x1b[<35;20;5m` 分 3 块到达），必须缓冲到完整序列再分发；处理 bracketed paste（`\x1b[200~`/`\x1b[201~`）、ESC 超时（本地 10ms / SSH 100ms）。
- 鼠标：`TuiAltScreen` 解析 SGR 鼠标（`parseSgrMouseEvent`），归一化成 `TuiMouseEvent {type: press|release|move|drag|click|wheel, x, y, screenX, screenY, width, height, shift/alt/ctrl, wheelDelta, clickCount}`，按提交的 layout frame 做 hit test（`getLayoutBoxesAt`/`getScrollViewsAt`），wheel 从最深 box 向根走、`chain` 或 `contain` 处理未消费 delta，无命中则交给 primary scroll view。

### 2.7 主屏 vs 备用屏

| | `TuiMainScreen` | `TuiAltScreen` |
|---|---|---|
| 终端 buffer | 主屏（保留 scrollback） | `\x1b[?1049h` 备用屏 |
| 滚动归属 | 终端 | 应用（`ScrollView`） |
| 渲染 | 行级 diff，必要时清屏全量重渲 | 固定 `terminal.rows` 视口，逐行比较后 in-place 更新 |
| 布局 | 无约束竖直文档 | `setLayoutRoot()` + VStack/HStack/ScrollView |
| 鼠标 | 不捕获 | 完整 SGR 鼠标、选择、超链接、滚动条 |
| 退出 | — | `\x1b[?1049l` 后**打印完整逻辑文档一次**（`ScrollView` 输出完整子内容，不是最后一帧） |
| 能力探测 | — | `isViewportTUI(tui)` 用 `Symbol.for("@earendil-works/pi-tui/viewport")` 判断 |

备用屏还额外实现：文本搜索面板（`alt-screen-search.ts`，327 行）、拖拽选择 + 边界自动滚动、OSC 8 超链接点击、OSC 52 复制、滚动条 hover/拖拽/点击跳转、`follow: "end"` 跟随 + "回到末尾"指示器、Kitty 图像纵向裁剪。

### 2.8 图像

`terminal-image.ts`（696 行）实现 **Kitty** 与 **iTerm2** 两种内联图协议（`ImageProtocol = "kitty" | "iterm2" | null`），能力探测优先看环境变量（`KITTY_WINDOW_ID`、`TERM_PROGRAM`、`ITERM_SESSION_ID`、tmux 转发探测、JetBrains JediTerm 特例），并支持 `setCapabilityOverrides`。

- 图像行本身是带特殊 APC/OSC 的"行"，TUI 用 `isImageLine()` 识别，`getKittyImageMetadata()` 拿保留行数。
- 备用屏下 Kitty 图支持**纵向裁剪**（视口顶部的图被裁）；iTerm2 因协议无法删除/裁剪 placement，在备用屏重绘时 **fallback 为文本**。
- 已知限制（`tui-plan.md` 第 609 行）：HStack 里的图像不做通用水平合成，允许要求图片独占整行。

### 2.9 native 模块

`native/` 下有 darwin/linux/win32 三个平台的 N-API 预编译 `.node`（源码为 `.m`/`.c`）。用途很窄：**剪贴板读写（含图片）** 和 **真实的修饰键状态查询**（`isModifierPressed`），Linux 通过 X11 helper 实现。`native-platform.ts`（63 行）延迟加载，加载失败返回 `undefined`；`getNativePlatformHelper()` 在非 darwin/win32 上直接返回 undefined。**渲染、按键、图像都不依赖 native**——native 缺失时剪贴板降级到 OSC 52、修饰键探测降级。

### 2.10 `tui-plan.md` 的设计意图（1,001 行）

它不是通用设计文档，而是一份"**实现交接单**"：给 `TuiAltScreen` 加**受约束布局系统**，把 coding-agent 的 transcript 做成可滚动区、pending/status/widget/editor/footer 做成底部固定 dock。核心决策（第 9–24 行）：

1. 约束布局是备用屏特性；`TuiMainScreen` 保持终端 scrollback 模型不变。
2. 两种模式共享**同一批组件实例**，只是 composition 不同。
3. 公开原语：`VStack`/`HStack`/`ScrollView`/overlays。
4. **布局树是内部实现，每帧重建；组件状态不重建**。
5. 复用既有叶子缓存，不引入第二层框架级缓存（"a second cache risks becoming stale because existing components own their invalidation semantics"）。
6. 只有 `requestRender()` 被调度后才重建布局帧。
7. `interactive-mode.ts` 的改动保持声明式且最小——布局、裁剪、滚动、hit test、事件路由都归 `packages/tui`。

其他重要设计点：`ViewportTUI` 能力接口（而非给所有 TUI 塞约束方法）；隐式 legacy root（只用 `addChild()` 的调用方自动得到一个 `ScrollView(primary, follow=end)` + 竖直文档，保持兼容）；clipping 时优先让含 `CURSOR_MARKER` 的行可见；小终端优先级（至少 1 行 transcript > 焦点光标 > 至少 1 行 footer > 裁剪 widget）；exit 时输出完整文档。

**注意：该 plan 已落地**——`src/layout.ts`、`components/{v-stack,h-stack,scroll-view,stack}.ts`、`src/layout-node.ts` 都在，相关测试也在 `test/layout.test.ts`、`test/tui-alt-screen.test.ts`。读它是为了理解**为什么**这样切分，而非待办。

### 2.11 Python 映射建议

pi-tui 的职责可拆成 6 层，Python 生态有不同成熟度的对应物：

| pi-tui 层 | TS 实现 | Python 建议 | 说明 |
|---|---|---|---|
| ANSI/宽度/grapheme | `utils.ts` 1,337 + `get-east-asian-width` | `wcwidth`/`wcwidth.wcswidth` + `regex` 或 `grapheme` 库；ANSI 解析可自写或借鉴 `rich.cells`/`prompt_toolkit.utils` | 必须自己封装 `visible_width/slice_by_column/truncate_to_width`，语义与 pi 对齐 |
| 终端抽象 + 协议协商 | `terminal.ts` 547、`stdin-buffer.ts` 444、`keys.ts` 1,401 | `prompt_toolkit.input`（含 VT100/Kitty 解析）+ 自写 Kitty/OSC 协商 | prompt_toolkit 的 `Vt100Parser`/key bindings 覆盖大部分；Kitty 协议支持需补 |
| 渲染循环 + 差分 | `tui.ts`、`tui-main-screen.ts` | `prompt_toolkit` 的 `Application(full_screen=False)` 自带 incremental renderer；备用屏用 `full_screen=True` | prompt_toolkit 的 renderer 会自己 diff；"退出时打印完整文档一次"需自定义 |
| 布局 | `layout.ts` 449、`VStack/HStack/ScrollView` | prompt_toolkit `Layout`+`HSplit`/`VSplit`/`Window`/`ScrollablePane`/`Float`；或 **Textual** 的 CSS 布局 | prompt_toolkit 更贴近 pi 的显式容器/显式尺寸 API；Textual 更接近重写 |
| 组件 | `Text/Markdown/Input/Editor/SelectList/...` | `rich` 渲染（Text/Markdown/Syntax/Table）→ prompt_toolkit `FormattedTextContainer`；编辑器/输入用 `prompt_toolkit.buffer.Buffer` + `KeyBindings` | `Editor`（2,461 LOC：kill-ring/undo/历史/autocomplete/粘贴标记）是最大块；prompt_toolkit 的多行 Buffer 覆盖大部分，Emacs kill/yank 与 undo 栈需自写 |
| 图像/剪贴板 | `terminal-image.ts`、native | 自写 Kitty/iTerm2 序列（参考 `term-image`/`textual-image`）；剪贴板用 `pyperclip`+OSC 52 | 图像可推迟到 v0.2 |

**关键判断：不建议从零实现 Python 的差分渲染器。** pi-tui 之所以手写渲染器（无 ink、无 blessed），是因为它要精确控制 alt-screen 视口、Kitty 图裁剪、OSC 133 跳转、选择/超链接语义——这些都是 pi 自身的产品需求，而不是通用 UI 需求。Python 移植若要"忠实"，应优先复用 `prompt_toolkit`（布局+renderer+editor+keybindings）和 `rich`（文本/Markdown 渲染），只把 **pi 特有语义**（alt-screen dock、follow-end、OSC 133、CURSOR_MARKER IME、exit full document）自写为适配层。反过来，如果目标是**逐行等价移植**（同一套 `Component.render(width)` 协议），则要准备承担 `utils.ts` + `keys.ts` + `tui.ts` + `layout.ts` + `editor.ts` 约 **6,600 LOC** 的等价实现。

---

## 3. `chord` — `@earendil-works/chord`

### 3.1 定位

5,822 LOC / 24 文件。README 明确写："**standalone application-composition runtime … it is not a Pi package: it does not depend on any other Pi workspace package**"。唯一运行时依赖是 `esbuild@0.28.2`（只用于 `./bundler` 与 `./node` 的 facet 打包/加载）。

### 3.2 概念

- **Facets / plugins**：`defineFacet({id, setup(env)})`。一个 plugin 可拆成多个 facet，分别打包进不同进程/环境（worker、TUI、browser）。`FacetEnvironment` 是插件全部 API：`use`（硬依赖单例）、`observe`（keyed 实例）、`provide`/`provideMany`、`replicatedState`、`own`（生命周期释放）、`onActivate`/`onDeactivate`。Host 在所有 facet 声明完形状后校验依赖图、先激活 provider 再激活 consumer、逆序 dispose。
- **Services**：`defineService<T>(id, {local:true})` 返回 frozen token；mode 为 `singleton` 或 `keyed`。`local:true` 允许任意 JS 合同；否则必须是 `RemoteServiceContract<T>`（编译期 JSON/void 校验）。consumer 拿"稳定 facade"，provider 断开/替换期间调用不崩。
- **Replicated state**：`replicatedState<T>(initial)`；producer 改 tracked proxy 后 `publish(context)`，每次 publish 只 flush 一个 op batch 并递增 sequence；consumer 收到完整不可变值，`ReplicatedStateReplica.update()` 遇 sequence gap 会抛错并清空直到 base batch 重新 hydrate。
- **Delta**（`./delta`，`src/delta/index.ts` **1,267 LOC**，另有 319 行 README）：**flush 时比较 tracked JSON 推导增量**，不保留 mutation history，所以内存只与 dirty graph 相关。Op 形如 `["r", value]`/`["s", path, value]`/`["d", path]`/`["a", path, text]`/`["t", path, count]`/`["p", path, index, remove, items]`；wire 形式增加 path interning（`["#", id, path]`，第二次使用才 intern）与 arity 省略。有严格安全校验（`RESERVED_SEGMENTS = {__proto__, constructor, prototype}`、`assertSafePath`、`PathError`）。文档化限制：单写者、有序投递、不支持对象标识复制/键序复制/稀疏数组/同一可变对象挂在两个 live path。
- **Context**（`./context`，`src/context/index.ts` **121 LOC**）：Go 风格不可变上下文链 + 取消：`Context { abortSignal, value(key) }`、`BACKGROUND_CONTEXT`、`TODO_CONTEXT`、`createContextKey<T>`、`withContextValue`、`withAbortSignal`/`withoutAbortSignal`、`withCancel`、`awaitWithContext`。
- **RPC / remote services**：chord 只定义 transport-independent 的 service wire grammar，**不规定 framing/routing/transport**。`RemoteServiceTransport { invoke, subscribe }` 由应用提供；`createRemoteServiceBinding`（consumer）、`createRemoteServiceEndpoint`（provider）处理 `$chord.service` 控制调用。对称 RPC peer 仍只在 PLANNING.md 里"计划中"。

### 3.3 公共导出

- `.`（`src/index.ts`，79 行）：`defineFacet`、`defineService`、`createFacetHost`、`createStaticFacetLoader`、`combineFacetLoaders`、`replicatedState`、`createRemoteServiceBinding`、`isJsonValue`、`RemoteServiceError`、`createServiceStateEncoder`/`Decoder`、`createServiceCatalogueCall`/`createServiceSubscribeCall`/`createServiceUnsubscribeCall`、`parseServiceCall`/`parseServiceCatalogue`/`parseServiceProviderUpdate`/`parseServiceSubscriptionSnapshot` 等，加约 30 个类型。
- `./context`（121 行）、`./delta`（1,267 行）、`./bundler`（9 行 re-export）、`./node`（24 行 re-export）。

### 3.4 依赖关系（关键）

依赖 chord 的包：`agent`、`client`、`coding-agent`、`protocol`、`server`（`packages/*/package.json` 实测）。

但**深度完全不同**：

- `pi-agent-core` 只在 `packages/agent/src/harness/context.ts:1-11` 运行时 import `@earendil-works/chord/context` 的取消/上下文原语（`awaitWithContext`/`withCancel`/`withAbortSignal`/`BACKGROUND_CONTEXT`/`createContextKey`…）。这些函数在 agent 核心被大量使用。另外两处（`harness/agent-harness.ts:1`、`harness/session/types.ts:1`）是 **type-only**（`JsonValue`/`JsonRepresentation`）。
- `coding-agent` 对 chord 的 34 个 import 文件**全部在 `src/experimental/**`、`src/cli/experimental/**`、`examples/plugins/**`**；非 experimental 代码 **零 import**。
- 打包证据：`scripts/build-coding-agent-bundle.mjs:18-27` 的 `allowedExternalPackages` 含 `@earendil-works/chord` 及各子路径，bundle 入口 `src/cli.ts` 从不 import `src/experimental/**`（`experimental/cli.ts` 不是 bundle 入口）。所以**发布二进制里没有 chord 内核，但 `./context` 会作为外部依赖随 `pi-agent-core` 加载**。

### 3.5 结论

**完整 chord 是 optional/advanced infra；只有 `./context`（121 LOC、零内部依赖）对一个单进程终端 coding agent 是必需的**，外加 `JsonValue`/`isJsonValue` 的严格 JSON 语义（可用于 Python 的 wire 校验）。facets/services/replicated state/delta/bundler 合计约 5,700 LOC 应整体推迟。

---

## 4. `protocol` / `client` / `server` — CBOR 远程协议栈

三者合计 **3,970 LOC src**，是 coding-agent 的 `devDependencies`（`packages/coding-agent/package.json:76-78`），只在 `PI_EXPERIMENTAL=1` 的 `experimental/` 路径下可用。`server` 的 README 自称 "**Experimental** local server for the new durable Session and Agent Harness interfaces"。

### 4.1 `protocol` — `@earendil-works/pi-protocol`（869 LOC / 8 文件）

- **Wire = 4 字节大端 uint32 长度前缀 + 一个 CBOR item**。`src/framing.ts:1` 定义 `FRAME_HEADER_LENGTH = 4`；`encodeFrame()` 手写大端序；`FrameDecoder` 增量处理任意分片/粘包，默认 `DEFAULT_MAX_FRAME_LENGTH = 16 MiB`，拒绝截断帧。
- **CBOR 是手写的**（`src/cbor/encoder.ts` 216 + `decoder.ts` 168 + `options.ts` 52），仓库里**没有 CBOR 依赖**。只支持 definite-length；拒绝 indefinite length、break、CBOR tags、非字符串 map key、重复 key；float 一律编成 64 位。默认限制：16 MiB 字节、1,000,000 容器元素、深度 64。
- **CBOR 与 JSON 是分层关系**：整帧是 CBOR，但 `call`/`result`/`update` 等 payload 字段是**不透明的 strict-JSON 值**；`src/codec.ts` 用 `Check(schema) && isJsonValue(value)` 校验，递归拒绝 NaN/Infinity/bytes/undefined/prototype/环。
- **Envelope**（`src/protocol.ts`，`PROTOCOL_VERSION = 8`）：
  - client→server：`{type:"hello", version}`（必须是首帧）、`{type:"request", id, target, call}`、`{type:"cancel", id, target}`
  - server→client：`{type:"hello", version:8, serverId}`、`{type:"hello_error"}`、`{type:"response", id, ok, result|error}`、`{type:"service_update", subscriptionId, update}`、`{type:"attachment", attachment}`
  - target 两级：`{serverId}`（server 级）与 `{serverId, sessionId, attachmentId}`（session 级）；`serverId` 是严格的小写 UUIDv4，`isServerId()` 校验。所有 envelope `additionalProperties:false`。
- 用的是 `typebox@1.3.27` 做 schema（Python 对应可用 `pydantic`）。

### 4.2 `client` — `@earendil-works/pi-client`（1,135 LOC / 8 文件）

- **API**（`src/index.ts`）：`Client`、`createClientServiceTransport`、错误 `ClientDisposedError`/`DisconnectedError`/`ServerError`、类型 `ByteTransport`/`ByteTransportFactory`/`ByteTransportHandlers`/`ClientOptions`/`ConnectionState`/`ServiceSubscription`/`AttachmentChangeListener`/`Unsubscribe`。子路径 `./unix`。
- `Client`（`client.ts:62`）：`static connect(options)` / `connect()` / `reconnect()` / `disconnect()` / `dispose()` + `Symbol.asyncDispose`；`request(target, call, signal)`、`serviceCatalogue(target)`、`subscribeService(target, serviceId, mode, listener)`；`onConnectionStateChange`/`onAttachmentChange`。
- 握手在内部 `Connection` 里：连上即发 `{type:"hello",version:8}`，要求 server hello 先到并校验 `serverId` 匹配，握手超时由 server 侧控制（默认 5s）。
- 订阅语义：subscribe 返回**完整快照**，安装 listener 之前到达的 update 会被 buffer，`start()` 后才按序投递；每个订阅一个独立的 chord `createServiceStateDecoder()`。**response id 必须匹配 pending request，否则视为致命协议错误并断连**。
- `./unix`：`discoverUnixServers()` 扫目录找 `<uuid>.sock`、`lstat().isSocket()`、最多 16 并发探测（默认 1s 超时）、忽略 stale/mismatch；`createUnixTransportFactory({path, maxPendingBytes})`（默认 pending = 4×16 MiB）；Windows 直接抛错。`ByteTransport.send(chunk)` 契约是按调用顺序、尊重背压。

### 4.3 `server` — `@earendil-works/pi-server`（1,966 LOC / 16 文件）

- `Server`（`server.ts:46`）：`constructor(host, options)`、`start()`、`accept(connection)`、`close()`、`serverId`。错误码：`wrong_server`、`session_not_found`、`session_ambiguous`、`session_not_attached`、`server_draining` + chord `RemoteServiceErrorCode`。
- `ServerHost { serverServices, resolveSession, openSession }`；`RoutedServerServiceHost.attachClient(presentation, ctx)`；`RoutedSessionHandle.attachClient`、`RoutedSessionAttachment.invokeService(call, publish, ctx)`。**真实的 `Session`/`AgentHarness` 始终进程内，不跨进程边界。**
- `SessionRouter`（312 行）：按 sessionId 路由，`requireAttachment` 强制 target 的 `{sessionId, attachmentId}` 与当前一致，否则 `SessionNotAttachedError`；按 client 串行化操作。
- 连接状态机：`awaitingHello → handshaking → ready → closing/closed`；协议错误转成 `hello_error` 或失败 `response`。
- **Transport 只有 Unix domain socket**（`src/transports/unix/`）：`createUnixListener`、`createUnixServer` preset、`getUnixSocketPath(serverId, dir) → <dir>/<serverId>.sock`，socket mode `0o600`，有 stale socket 检测、`maxPendingBytes`。**server 包里没有 stdio transport。** WebSocket "radius" relay 在 coding-agent 侧（`src/experimental/radius-relay.ts`，子协议 `pi-session-relay.{host,client}.v1`）。
- **没有固定 RPC 方法表**：请求携带的是 chord service call（`{serviceId, instance?, member, args}`）。实际使用的 serviceId（coding-agent 定义）：server 级 `pi.session-directory`、`pi.session-management`（`create`/`remove`/`attach`/`detach`）、`pi.presentation-plugins`；session 级 `pi.models`、`pi.agent-controller`、`pi.transcript`、`pi.session-plugins`。

### 4.4 它和 `pi --mode rpc` 是两回事

| | CBOR client/server 栈 | stdin/stdout JSONL RPC 模式 |
|---|---|---|
| 包 | `pi-protocol`+`pi-client`+`pi-server`+chord | coding-agent 内部（`src/modes/rpc/*`，无独立包） |
| 入口 | `pi experimental server` / `pi client`（需 `PI_EXPERIMENTAL=1`） | `pi --mode rpc`（另有 `src/rpc-entry.ts`） |
| Transport | Unix socket（树内）；WebSocket relay（coding-agent 侧） | 子进程 stdin/stdout 管道 |
| Framing | 4 字节大端长度前缀 | LF 分隔的文本行，每行一个 JSON |
| 编码 | 手写 CBOR envelope + 不透明 strict-JSON payload | 纯 UTF-8 JSON |
| 握手 | 强制 hello/hello，version 8 + 逻辑 serverId | 无 |
| 多路复用 | 多 client / 多 session / attach-detach / attachmentId / cancel / 订阅 | 一进程一 session，request id 可选 |
| 流式 | `service_update`（chord replicated state）+ 带外 `attachment` | agent events 作为 JSONL 写 stdout |
| 用途 | 远程/持久 session，多 presentation（TUI/编辑器/自定义 UI） | 嵌入/无头控制单个 agent 子进程（IDE、脚本、扩展） |
| 状态 | 实验性，protocol version 8，无兼容保证 | 稳定，有 `docs/rpc.md` 文档 |

`src/modes/rpc/jsonl.ts` 特别注明**不能**用 Node `readline`（它会在 U+2028/U+2029 上错误分行），必须只按 `\n` 分行并去掉尾部 `\r`。Python 侧对应注意：不要用会按 Unicode 行分隔符切分的读取器。

### 4.5 结论

protocol/client/server 使能的是"**远程/分离式 session + 多 presentation**"，属于高级/实验特性。Python v0.1 **不需要** CBOR 栈；若未来要做远程，需要同时移植 chord 的 service-call 语法与 replicated-state delta 编解码（因为 `pi-protocol` 本身只校验 envelope）。`pi --mode rpc` 的 JSONL 模式才应该是 v0.1 的"可嵌入"接口。

---

## 5. `session-backends/sqlite-node` — `@earendil-works/pi-session-backend-sqlite-node`

`packages/session-backends/` 下只有 `sqlite-node` 一个子包。version `0.85.1`，描述 "Node sqlite session backend for @earendil-works/pi-agent-core sessions"。依赖 `pi-ai` + `pi-agent-core`；用 Node 22 内置 `node:sqlite`（`DatabaseSync`），**不需要原生模块或 better-sqlite3**。0.84.0 从 `@earendil-works/pi-storage-sqlite-node` 改名（breaking，旧库不迁移）。

**体量**：src 15 文件 **1,973 LOC TS + 121 LOC SQL**。最大文件 `sqlite/repo.ts` 453、`sqlite/session/branch-entries.ts` 324、`session.ts` 213、`storage.ts` 211。

### 5.1 它加在 JSONL store 之上的是什么

JSONL store 在 `packages/agent/src/harness/session/jsonl/`（约 1,894 LOC）：每 session 一个 `{ISO-ts}_{encodeURIComponent(id)}.jsonl`，format-4 header + 追加式事务行，`InMemoryStorageState`（349 LOC）把整个 session 放内存，**所有 scan 都是内存 JS 过滤**；有 torn-tail 检测/恢复、快照重写、legacy v3 导入，**没有锁**。

SQLite 后端的增量能力：

- **真正的 SQL 查询**：SQL 侧过滤、游标、`LIMIT`；`scanBranch` 用 `branch_meta.base_seq` 把祖先拆段，跑带索引的 `branch_entries CROSS JOIN entries`。
- **覆盖索引**支持按序/按类型的 branch 扫描；持久化 `next_seq` 做共享序列分配。
- **物化统计**：`sessions.message_count` + `usage_payload` 让 `getStats` 很便宜；独立的 `usage_ledger` 表支撑 `scanUsage`。
- **branch 投影**（`branch_entries`/`branch_meta`）把分支/祖先扫描做成一级概念而非每次重算。
- **SQL 层不变量**：两个触发器 `trg_entries_validate` / `trg_usage_ledger_validate` 强制父条目先插入、以及 entry/usage id 共享命名空间。
- WAL（`PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000`）、单文件-per-session 或共享多 session 容器（`databasePath`）、只读打开/发现、显式 `storageVersion` 门控。
- 已知短板：**没有跨进程 lease/lock/fence/heartbeat**（README 明说"host lifecycle, not this backend, guarantees one writable owner per Session"），**没有 FTS/search**。

### 5.2 Schema（`src/sqlite/migrations/001_initial.sql`，唯一的 migration）

| 表 | 关键列 | 主键 |
|---|---|---|
| `sessions` | `id, created_at, parent_session_id, storage_version, metadata, message_count, usage_payload, next_seq` | `id`（WITHOUT ROWID） |
| `entries` | `session_id, id, parent_id, seq, type, custom_type, timestamp, payload` | `(session_id, id)` |
| `scalar_values` | `session_id, namespace, key, seq, value` | `(session_id, namespace, key)` |
| `list_values` | `session_id, namespace, key, seq, value` | `(session_id, namespace, key, seq)` |
| `usage_ledger` | `session_id, id, seq, entry_id, adjustment, usage, details` | `(session_id, id)` |
| `branch_entries` | `session_id, branch_id, entry_id, entry_seq, entry_type` | `(session_id, branch_id, entry_id)` |
| `branch_meta` | `session_id, branch_id, tip_entry_id, tip_seq, base_branch_id, base_seq` | `(session_id, branch_id)` |

7 个表、7 个索引（含 unique `ix_bm_tip`）、2 个触发器。文件头注释：storage format 4 / storageVersion 1；权威状态是 `entries + scalar_values + list_values + usage_ledger`，`branch_*` 与 `sessions` 上的统计列是维护出来的投影/缓存。

### 5.3 公共 API 与抽象层次

- 抽象接口在 `pi-agent-core`：`packages/agent/src/harness/session/types.ts` 定义 `Storage`、`Session`、`Branch`、`SessionRepo<TMetadata,TCreateOptions,TListOptions>`（create/open/list/delete/fork）。
- 本包是**一个具体实现**：`createNodeSqliteFactory()`、`wrapNodeSqliteDatabase(db)`，加 `SqliteSessionRepo`（453 行）、`SqliteOpenSession`、`SqliteStorage`、`SqlQuery`+`sql` 模板标签。注入 `SqliteDatabaseFactory`，因此理论上可换引擎。
- 内置的其他实现：`JsonlSessionRepo`、`MemorySessionRepo`。

### 5.4 是否可选

**完全可选。** 证据：(1) 除自身外**没有任何 package.json 依赖它**；(2) 没有 `sessionBackend`/`storageBackend`/`--backend` 之类的配置项或 CLI flag（grep 只找到无关的 `AuthStorageBackend`）；(3) coding-agent 的 `package.json` 里没有它，CLI 走死 `JsonlSessionRepo`；(4) 选择后端只能靠调用方自己 `new SqliteSessionRepo({directory, databaseFactory: createNodeSqliteFactory()})`。

**Python 移植建议**：Python 有 `sqlite3` 标准库，移植这个后端比 TS 侧还便宜，而且它解决的正是 JSONL 的内存扫描/并发问题。但它**不是 v0.1 阻塞项**——可以先做 JSONL（忠实），把 SQLite 作为 v0.2 的 pluggable backend，并把 `Storage`/`Session`/`SessionRepo` 抽象接口在 v0.1 就定好（这是廉价且高价值的解耦）。

---

## 6. `telemetry` — `@earendil-works/pi-telemetry`

**935 LOC / 6 文件**。vendor-neutral 的**埋点契约 + 类型化 schema 工具**，不是 exporter：

- 契约：`TelemetryContext`/`TelemetrySpan`（callback 风格）、共享 `NOOP_TELEMETRY_CONTEXT`、参考实现 `InMemoryTelemetryContext`（`src/memory.ts` 219 行）。
- 类型化：`defineTelemetrySchema`（72 行）、`createTypedSpanStarter`（349 行），以及一整套 `Infer*` 推导类型；`./testing` 子路径导出 adapter conformance 套件（315 行）。
- 依赖方只有 `packages/agent` 和 `packages/ai`（都是 runtime 依赖），但它本身**零内部依赖**。
- 实际是否必需：消费方永远有兜底——`packages/agent/src/harness/context.ts:31` 是 `context.value(TELEMETRY_CONTEXT_KEY) ?? NOOP_TELEMETRY_CONTEXT`，所以**不配 backend 也能跑**；仓库里只发布了 in-memory 参考实现，没有 OTLP exporter。

**Python 建议**：v0.1 用 no-op stub 即可（一个 `TelemetryContext` Protocol + noop 实现 + 可选的 in-memory recorder 用于测试断言）。若想要 OpenTelemetry 兼容，把它映射到 `opentelemetry-api` 的 span 接口，不必移植这套类型推导。

---

## 7. `evals` — `@earendil-works/pi-evals`

**1,351 LOC / 9 文件**，`"private": true`，纯开发期评测：

- 入口 `scripts/run-evals.mjs`（97 行），解析 `--provider`/`--model`（或 `PI_PROVIDER`/`PI_MODEL`，两者必须成对），建 `.eval/<ISO-timestamp>_<uuid>/`（mode 0700）产物目录，然后用 `spawnSync` 跑 Vitest，产出 `runs.jsonl` + 原生 pi session JSONL 快照（README 警告内含 prompt/响应/源码/工具输出）。
- `src/pi-harness.ts`（261 行）把**真实** `AgentSession` 适配成 `vitest-evals` 的 `createHarness`（隔离 temp project/agent dir）。
- 评测内容：`smoke.eval.ts`（17 行，端到端 prompt + usage 断言）、`docs.eval.ts`（70 行，用受约束的 `submit_documentation_audit` 工具审计每篇 `docs/**/*.md` 与实际实现是否一致）、`extensions.eval.ts`（140 行，生成/加载/reload/使用 `.pi/extensions/hello.ts`）。
- **不进任何构建或运行时依赖图**（只有一处 version-sync 测试提及包名）。

**Python 建议：不移植**。它依赖真实模型与 coding-agent 的扩展系统。可以把它当作"**如何验证一个移植**"的方法论参考（smoke + 文档一致性审计 + 扩展生命周期），用 pytest 重写同类用例。

---

## 8. 整体架构

### 8.1 依赖分层（实测自各 `package.json`）

```
                    (无内部依赖)
        ┌───────────────┬──────────────┬──────────────┐
        │  telemetry    │    tui       │   chord      │
        └───────┬───────┴──────┬───────┴──────┬───────┘
                │              │              │  (esbuild)
        ┌───────┴───────┐      │      ┌───────┴────────────┐
        │      ai       │      │      │ protocol (→chord)  │
        └───────┬───────┘      │      └───────┬────────────┘
                │              │              │
        ┌───────┴──────────────┴────┐   ┌─────┴──────┐
        │  agent  (→chord, ai, tel) │   │  client    │
        └───────┬───────────────────┘   │ (→chord,   │
                │                       │  protocol) │
        ┌───────┴───────────────────────┴────────────┴┐
        │        coding-agent                         │
        │  deps: chord, agent, ai, tui                │
        │  dev : client, protocol, server             │
        └─────────────────────────────────────────────┘

        sqlite-node (→ ai, agent)  ← 无任何包依赖它（孤立可选叶子）
        evals       (dev→ ai, coding-agent)  ← private，孤立
```

显式边（`packages/*/package.json`）：

```
pi-agent-core      dep → chord, pi-ai, pi-telemetry
pi-ai              dep → pi-telemetry
pi-client          dep → chord, pi-protocol
pi-coding-agent    dep → chord, pi-agent-core, pi-ai, pi-tui
                   dev → pi-client, pi-protocol, pi-server
pi-protocol        dep → chord
pi-server          dep → chord, pi-agent-core, pi-protocol
pi-session-backend-sqlite-node dep → pi-ai, pi-agent-core
pi-evals           dev → pi-ai, pi-coding-agent
pi-telemetry / pi-tui / chord  → 无内部依赖
```

要点：

- **`tui` 是纯叶子**（零内部依赖），只被 `coding-agent` 使用 → 移植时可以独立成库、独立测试。
- **`chord` 名义上是叶子，但 `agent → chord/context` 让"取消/上下文"成为核心依赖**；这是 chord 唯一不可回避的部分。
- **`protocol/client/server` 形成一条独立侧链**，只被 coding-agent 的实验入口引用，且是 devDependencies。
- **`sqlite-node` 是"实现了 agent 定义接口但无人使用"的孤立叶子**——这既是可选性的证据，也是 pluggable backend 设计已完成的证据。
- **`telemetry` 被 ai 和 agent 依赖，但永远可退化为 no-op**。
- `scripts/check-entry-graphs.mjs` 用预算约束每个 `exports` 入口的 import 图规模，说明分层是**被 CI 强制的工程契约**，不是文档约定。

### 8.2 复杂度热点（最大文件，LOC）

`tui`（18,107）：

| 文件 | LOC |
|---|---|
| `components/editor.ts` | 2,461 |
| `tui-alt-screen.ts` | 1,721 |
| `tui.ts` | 1,456 |
| `keys.ts` | 1,401 |
| `latex.ts` | 1,394 |
| `utils.ts` | 1,337 |
| `components/markdown.ts` | 1,015 |
| `autocomplete.ts` | 826 |
| `terminal-image.ts` | 696 |
| `tui-main-screen.ts` | 655 |
| `terminal.ts` | 547 |

前 6 个文件 = 9,770 LOC，占 tui 的 **54%**。结论：**tui 的复杂度主要在编辑器、alt-screen 渲染、按键解析、ANSI/宽度工具、LaTeX**，而不是组件库本身。

`agent`（25,305）：`harness/runtime/lane.ts` 2,012、`harness/runtime/drive/structural.ts` 1,222、`harness/session/testing/conformance/{session-repo,storage}.ts` 1,185+920、`harness/env/nodejs.ts` 924、`harness/compaction/compaction.ts` 865、`agent-loop.ts` 803、`harness/runtime/drive/tools.ts` 692、`harness/session/jsonl/legacy-v3.ts` 681。

`ai`（24,384）：`api/openai-completions.ts` 1,717、`api/openai-codex-responses.ts` 1,657、`api/anthropic-messages.ts` 1,490、`api/bedrock-converse-stream.ts` 1,325、`models.ts` 957、`api/mistral-conversations.ts` 941、`types.ts` 881、`api/openai-responses-shared.ts` 793。**复杂度是 provider 数量，不是抽象**。

`coding-agent`（70,052）：`modes/interactive/interactive-mode.ts` **6,620**、`core/agent-session.ts` 3,552、`core/package-manager.ts` 2,699、`core/extensions/types.ts` 1,797、`core/session-manager.ts` 1,746、`modes/interactive/components/tree-selector.ts` 1,427、`core/settings-manager.ts` 1,417、`core/extensions/runner.ts` 1,286、`modes/interactive/theme/theme.ts` 1,234。按目录：`core` 18,932、`modes/interactive` 7,069、`experimental` 5,620（其中 services 1,520）、`utils` 3,544、`modes/rpc` 1,785、`cli` 1,416、`extensions/llama` 1,453、`core/tools` 3,386、`core/extensions` 4,130。

**最值得注意的反直觉事实**：`interactive-mode.ts` 单文件 6,620 LOC，是全仓库最大文件——交互模式的复杂度主要不在 UI 库（tui 的 alt-screen 只有 1,721），而在"把 session/工具/扩展/主题/选择器/队列/widget 全部编排起来"。

### 8.3 移植可行性的三个结构判断

1. **`ai` + `agent` + `coding-agent` + `tui` 是 137,848 LOC 的庞然大物**，其中真正可复用的"框架"是 `tui`(18k) + `agent/harness`(~12k) + `ai` 的 provider 抽象(~3k)，其余是 provider 适配与产品特性。Python 移植不能按 LOC 等比投入。
2. **周边包中只有 `tui` 与 `chord/context` 进了发布二进制的运行路径**（`tui` 直接被 coding-agent 依赖；`chord/context` 经 agent 传递，且被 bundler externalize）。`protocol/client/server/sqlite/telemetry/evals` 都不在 `src/cli.ts` 的 entry graph 上。
3. **分层是被工具强制的**：`check-entry-graphs.mjs` 的预算、`check-runtime-deps.mjs`、pin 死的直接依赖、`AGENTS.md` 的 erasable-syntax 规则，共同意味着**移植时把"包边界"抄对，比把实现抄全更重要**。

---

## 9. Python 移植顺序（含 v0.1 取舍）

### 9.1 建议顺序与理由

**Phase 0 — 基础工具（自写，~1,500 LOC 等价）**
`visible_width` / `slice_by_column` / `truncate_to_width` / `wrap_text_ansi` / grapheme 切分 / ANSI 解析。理由：`tui`、`coding-agent` 的所有渲染都建立在这套语义上；这些函数的边界行为（CJK、emoji、组合字符、区域指示符）是 pi 有专门回归测试的地方（`test/wrap-ansi.test.ts`、`regression-regional-indicator-width.test.ts`）。**必须最先做，且必须有等价测试**。

**Phase 1 — `pi-ai` 子集（v0.1 只做 1–2 个 provider）**
先做 provider 无关的 `types.py` + 流式事件模型，再做 OpenAI-compatible（`openai-completions`）和/或 Anthropic Messages。理由：`ai` 有 12 个 provider 文件，全做等于把 24k LOC 抄一遍；但对一个可用 agent 来说，2 个 provider 足够。模型目录（`models.generated.ts`，957 行）可用脚本从上游生成。

**Phase 2 — `pi-agent-core`：agent loop + session store（JSONL）**
`agent-loop`、工具调用协议、上下文管理、compaction、`JsonlSessionRepo` + `MemorySessionRepo`，并在**这一层就定好** `Storage`/`Session`/`SessionRepo` 抽象（对应 TS 的 `harness/session/types.ts`）。同时实现 `chord/context` 等价物（见下）。
理由：这是 agent 的"引擎"；JSONL 是默认且唯一的实际后端，忠实移植即可；抽象接口先定好，SQLite 才能后补而不返工。

**Phase 3 — `pi-tui` 核心**
建议分两步：
- 3a（v0.1 必需）：`Text`/`Box`/`Container`/`Spacer`/`TruncatedText`、`Markdown`（用 `rich` 或 `markdown-it-py` + 自写 ANSI 输出）、`Input`、`Editor`（可用 `prompt_toolkit.buffer.Buffer` 承载，但 kill-ring/undo/历史/粘贴标记要对齐）、`SelectList`、`Loader`、`fuzzy`、主屏渲染循环（用 `prompt_toolkit` 的 incremental renderer）。
- 3b（v0.1 可选 / v0.2）：`TuiAltScreen` 等价物（alt buffer、固定 dock、`VStack`/`HStack`/`ScrollView`、follow-end、鼠标/选择/超链接、OSC 133、搜索）、`Image`（Kitty/iTerm2）、`SettingsList`、overlays、`latex`。

理由：`Editor`(2,461) + `Markdown`(1,015) + 工具(1,337) 是"日常可用"的最小集合；alt-screen 布局系统（`tui-plan.md` 那一整套）是**体验升级**而非可用性前提。主屏模式的竖直文档已经能跑出一个完整 coding agent。

**Phase 4 — `pi-coding-agent` 骨架**
`core/tools`（read/edit/bash/grep/find…）、`core/session-manager`、`core/settings-manager`、CLI 参数、interactive mode（先做主屏版）、JSONL RPC 模式（`--mode rpc`）。理由：这是把前面所有部件接成人能用的产品；RPC JSONL 模式（`src/modes/rpc/`，1,785 LOC）比 CBOR 栈简单一个数量级，却提供了同样的"可嵌入"价值。

### 9.2 v0.1 应包含 / 应推迟

**v0.1 必做**

| 来源 | 内容 | 理由 |
|---|---|---|
| 自写 | 宽度/ANSI/grapheme 工具层 | 一切渲染的地基，且有微妙边界 |
| `ai` | provider 抽象 + 1–2 个 provider + 流式 | 没有 LLM 就没有 agent |
| `agent` | agent loop、工具调用、`Storage`/`Session`/`SessionRepo` 接口、JSONL + memory 后端、compaction | 核心引擎；接口先定好避免返工 |
| `chord` | **仅** `./context` 等价物：不可变 context 链 + abort/cancel + `await_with_context`（约 121 LOC 的等价物），与 `JsonValue`/严格 JSON 校验 | agent 运行时硬依赖它 |
| `tui` | 工具层、`Text/Markdown/Box/Container/Input/Editor/SelectList/Loader/Spacer/TruncatedText/fuzzy`、主屏渲染模式 | 终端 agent 的最低可用 UI |
| `coding-agent` | 工具集、session 管理、settings、CLI、interactive（主屏）、`--mode rpc` JSONL | 把引擎接成产品；JSONL RPC 提供嵌入能力 |
| `telemetry` | no-op `TelemetryContext` + in-memory recorder | 保持接口形状，成本极低 |

**推迟到 v0.2+（按优先级）**

1. **SQLite session backend**（`session-backends/sqlite-node`，2,094 LOC 等价）：有标准库 `sqlite3` 很好写，但 JSONL 已够用；等 session 变大/需要并发再上。**v0.1 只需把接口留出来。**
2. **alt-screen 布局系统**（`tui-plan.md` 全套 + `layout.ts` + VStack/HStack/ScrollView + 鼠标选择/超链接/搜索/图像）：体验升级，非可用性前提。
3. **内联图像（Kitty/iTerm2）与 native 剪贴板**：小众人群；可先用文本 fallback + OSC 52。
4. **扩展系统**（`core/extensions` 4,130 + `package-manager` 2,699 + `extensions/types.ts` 1,797）：pi 的"self-extensible"卖点，但与忠实移植 agent 核心无关，且是最大的产品复杂度来源之一。
5. **`chord` 完整运行时**（facets/services/replicated state/delta/bundler，~5,700 LOC）：只有当要做多进程 / 远程 / WebUI 时才需要。
6. **`protocol` + `client` + `server`（CBOR 栈，3,970 LOC）**：实验性、无兼容保证、定义了 protocol version 8；且单独移植没意义——它依赖 chord 的 service grammar。等有明确的"远程 session / 多客户端"需求再做，且届时可以评估是否直接用 JSON-RPC/WebSocket 替代手写 CBOR。
7. **`evals`**：不移植；只借鉴其"smoke + 文档一致性审计 + 扩展生命周期"的验证方法，用 pytest 重写。

### 9.3 一个具体的"忠实度"取舍建议

pi-tui 提供了两种移植目标，团队需要先选：

- **接口忠实**（推荐）：Python 侧保留 `Component.render(width) -> list[str]` 协议、`VStack/HStack/ScrollView` 语义、`follow: "end"`、`CURSOR_MARKER`、`TUI_KEYBINDINGS` 表。底层渲染可以交给 `prompt_toolkit`/`rich`，但**不改上层语义**。这样 `coding-agent` 的组件（43 个 interactive 组件）可以被近乎机械地翻译，且未来可对齐上游行为。
- **实现忠实**（不推荐）：连差分渲染器、ANSI 切片算法、Kitty 解析都逐行重写。成本是 `utils.ts`+`keys.ts`+`tui.ts`+`layout.ts`+`editor.ts` ≈ 6,600 LOC 的等价实现，回报仅是"更少的第三方依赖"。

无论选哪种，**`chord/context`（121 LOC 的等价物）和严格 JSON 校验都必须在 v0.1 就位**，因为 agent 运行时的取消/上下文语义贯穿整个 `harness`。而 `protocol/client/server`、facets、evals 可以完全不做。

---

## 附录 A：本报告关键路径索引

| 主题 | 路径 |
|---|---|
| tui 公共导出 | `/tmp/pi-ref/packages/tui/src/index.ts` |
| tui 渲染循环/焦点/overlay | `/tmp/pi-ref/packages/tui/src/tui.ts` |
| 主屏 diff 渲染 | `/tmp/pi-ref/packages/tui/src/tui-main-screen.ts` |
| alt-screen 渲染 + 鼠标/选择 | `/tmp/pi-ref/packages/tui/src/tui-alt-screen.ts` |
| 布局树/裁剪/paint | `/tmp/pi-ref/packages/tui/src/layout.ts` |
| 布局节点协议 | `/tmp/pi-ref/packages/tui/src/layout-node.ts` |
| stack 分配器 | `/tmp/pi-ref/packages/tui/src/components/stack.ts` |
| ScrollView | `/tmp/pi-ref/packages/tui/src/components/scroll-view.ts` |
| 按键解析（Kitty） | `/tmp/pi-ref/packages/tui/src/keys.ts` |
| keybinding 注册表 | `/tmp/pi-ref/packages/tui/src/keybindings.ts` |
| 终端抽象 + 协议协商 | `/tmp/pi-ref/packages/tui/src/terminal.ts` |
| ANSI/宽度/grapheme | `/tmp/pi-ref/packages/tui/src/utils.ts` |
| 图像协议 | `/tmp/pi-ref/packages/tui/src/terminal-image.ts` |
| alt-screen 设计交接单 | `/tmp/pi-ref/tui-plan.md` |
| chord 公共 API | `/tmp/pi-ref/packages/chord/src/index.ts` |
| chord context | `/tmp/pi-ref/packages/chord/src/context/index.ts` |
| chord delta | `/tmp/pi-ref/packages/chord/src/delta/index.ts` |
| agent 对 chord 的唯一运行时依赖 | `/tmp/pi-ref/packages/agent/src/harness/context.ts` |
| 协议 envelope | `/tmp/pi-ref/packages/protocol/src/protocol.ts` |
| 4 字节帧 | `/tmp/pi-ref/packages/protocol/src/framing.ts` |
| CBOR 手写实现 | `/tmp/pi-ref/packages/protocol/src/cbor/` |
| client | `/tmp/pi-ref/packages/client/src/client.ts` |
| server | `/tmp/pi-ref/packages/server/src/server.ts` |
| server unix transport | `/tmp/pi-ref/packages/server/src/transports/unix/listener.ts` |
| sqlite schema | `/tmp/pi-ref/packages/session-backends/sqlite-node/src/sqlite/migrations/001_initial.sql` |
| session 抽象接口 | `/tmp/pi-ref/packages/agent/src/harness/session/types.ts` |
| JSONL store | `/tmp/pi-ref/packages/agent/src/harness/session/jsonl/` |
| telemetry 契约 | `/tmp/pi-ref/packages/telemetry/src/index.ts` |
| JSONL RPC 模式（非 CBOR） | `/tmp/pi-ref/packages/coding-agent/src/modes/rpc/jsonl.ts` + `docs/rpc.md` |
| 包边界 CI 强制 | `/tmp/pi-ref/scripts/check-entry-graphs.mjs` |
| 独立二进制构建 | `/tmp/pi-ref/scripts/build-binaries.sh` |
| bundle externalize 列表 | `/tmp/pi-ref/scripts/build-coding-agent-bundle.mjs` |

## 附录 B：本报告未覆盖的部分

`packages/ai`、`packages/agent`、`packages/coding-agent` 三个核心包的内部 API 面、provider 实现细节、工具实现、扩展系统、主题系统不在本报告范围内（本报告只提供它们的体量、最大文件与依赖边，作为分层依据）。`chord/PLANNING.md`（860 行）中"计划中"的对称 RPC peer、server 的 WebSocket radius relay 细节、以及 `coding-agent/src/experimental/mini/**` 也未展开。
