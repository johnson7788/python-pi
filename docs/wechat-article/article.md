# python-pi：开源的多租户 coding agent harness

> python-pi 的上游是 pi，一个用 TypeScript 写的终端 coding agent，11 个 npm 包、约 18 万行。我们用 Python 重写了它，保留语义契约、会话文件格式和命令协议，另外加了四样上游没有的东西：浏览器 UI、团队多租户、容器沙盒、治理与审计。代码量从 18 万行降到 25.2k 行，588 个测试全部离线运行。
>
> 文章分三部分：继承了什么，改进了什么，自己做了什么。

## 一、继承

pi 有四个设计决定值得照搬。

### 1.1 分层清楚，每层都能单独用

![分层架构](assets/arch.png)

- pi-ai（5.6k 行）：provider 抽象。消息和内容块类型、12 种流式事件、provider 注册表、Anthropic 和 OpenAI 兼容适配器、工具参数校验、成本计算。
- pi-agent-core（1.8k 行）：agent 循环和 Agent 状态机。这一层只有 1.8k 行，可以跳过编码助手直接拿来驱自己的工具。
- pi-coding-agent（14.7k 行）：具体业务。JSONL v3 会话树、7 个内置工具（read/write/edit/bash/grep/find/ls）、系统提示词、skills、settings、会话编排、print/json/rpc 三种模式、扩展协议。
- pi-server（3.1k 行）：我们加的，见第三节。

依赖方向单一，任意一层都可以单独用。核心三层只引入两个第三方包，httpx 和 pyyaml，pi-agent-core 一个新依赖都没加。上游用到的 typebox、partial-json、proper-lockfile 这些，我们分别用内联实现、json 容错解析和标准库替代了。能自己写三十行解决的事，就不装包。

### 1.2 会话存成一个文件

![会话文件与分叉](assets/session-tree.png)

会话是 JSONL，version 3，用 id 和 `parentId` 组成一棵树。分叉就在原文件里发生：继续 append，新记录的 `parentId` 指回分叉点，不新建文件。只有 `fork()` 会把 root 到某个节点的路径复制进一个新文件。另外有个怪癖得照抄：第一条 assistant 消息落盘之前，不创建文件。

会话是这类工具最贵的数据。存成可读、能 diff、能手工改的文本，出问题的时候可以直接打开看，不用先连数据库再写查询。

### 1.3 流式事件有明确的契约

一共 12 种事件。成功是 start → updates* → done，生成中途失败是 start → updates* → error，setup 阶段就失败则直接发 error，没有 start。

最反直觉的一条是 partial：它是所有事件共享的同一个可变对象，原地修改，不是每帧一份快照。如果你把 `event.partial` 存进列表，想最后统一处理，拿到的会是同一个对象的最终状态，前面几帧的内容都丢了。这种做法省内存，但对消费者有要求。上游选择保留，我们也保留。

### 1.4 上游把行为边界写死了，所以语义能逐字对齐

这是能做成这件事的前提。pi 把每个行为的边界都定死了，两个 API 的错误处理方式可以相反，同一个项目里可以有两种时间戳单位。这些不是随手写的，都是被测试钉住的可观察契约。照着它们走，断言能直接抄上游测试；自己想当然发挥，每个偏差都要另外设计验证。

## 二、改进

### 2.1 只搬核心

改的第一步是克制。把上游源码逐层读完之后，有三个发现决定了边界：

- 裸 agent 循环是自包含的。types、agent-loop、agent、stream-fn 四个文件一共约 1.6k 行，对 harness 目录零 import。harness 那约 12k 行是附加的持久化运行时，不是必需品。
- chord 的完整内核只在实验代码里用。experimental 目录之外，没有非实验代码 import 它。
- CBOR 远程栈和 `--mode rpc` 是两套协议。前者是实验性的，后者才是稳定的集成方式。

按这三条推，不搬的东西就定下来了：TUI（18k 行）、harness 持久化运行时、CBOR 协议栈、SQLite 会话后端、telemetry、evals。18 万行变成 3 万行，差别不在能力，在于刻意不搬。

### 2.2 落到 Python 生态

能嵌进 Python 技术栈。上游只能嵌进 Node 服务，现在四层库 `uv add` 就能用。仓库里有个完全离线的例子，真实工具、真实会话落盘，用确定性 provider 代替 LLM，跑 uv run python examples/offline_agent.py 就行。

扩展系统改成了 Python 原生的。上游扩展是 TypeScript，我们没有做跨语言兼容。这是有意的：扩展 API 里 renderer 返回组件、同进程 `AbortSignal`、参数原地修改这些语义，没法跨 RPC 边界存活，硬做只能做出一个残缺子集。所以 Python 侧另写了一套插件协议，一个普通模块暴露 `setup(pi)`，就能注册工具、命令或者拦截事件。拦截一次 bash 调用大概十来行。

### 2.3 不该改的地方一条没改

这是全文最要紧的一节。改进的前提是不许乱改，16 条可观察契约我们一条没动。挑四条最反直觉的说：

**① `stream()` 不会因为 setup 失败抛异常**，它把失败编码成一个 error 事件；而 `stream_simple()` 可以同步抛。同一个库里的两个 API，错误处理方式故意相反，因为首次配置检测依赖同步抛出。你要是为了统一风格把它们改成一致，调用方的初始化探测会静默失效。

**② `stop_reason` 等于 "length" 时，整批工具调用都不执行。** 不是执行一部分，也不是执行到哪算哪，是全部报错，让模型重新发起。

**③ 两处 hook 的异常后果不同。** `before_tool_call` 抛异常会变成一个 error tool result，`after_tool_call` 抛异常则结果被丢弃。

**④ Python 最容易踩的一个。** `dict.get()` 把「显式 null」和「没有这个键」合并了，但它们的语义不一样：

```python
# 错：把「显式 null」和「没有这个键」当成一回事
if mapping.get("minimal"):
    ...

# 对：键不存在 = 用 provider 默认；显式 null = 不支持
if "minimal" in mapping and mapping["minimal"] is not None:
    ...
```

其余 12 条里最容易被顺手改掉的：

- `EventStream` 是 push 队列，完成事件本身仍要投递给迭代方，`result()` 在首个 done 或 error 时就 resolve，不等 `end()`；
- `terminate` 只有批次内每一个结果都满足时才终止循环；`AbortSignal` 是被观察的标志，不是 `CancelledError`；
- 并行工具执行时，`tool_execution_end` 按完成顺序发，`toolResult` 消息按源顺序装配；
- 消息的时间戳是毫秒整数，会话记录的时间戳是 ISO 字符串，同一个项目里两种单位。

## 三、创新

前面是把别人的东西搬过来，这一节是上游没有的。

### 3.1 一套协议，两种传输

![一次 prompt 的链路](assets/flow.png)

上游只有终端 TUI 和 stdio 上的 `--mode rpc`。我们没有重写那 18k 行终端差分渲染器，而是补了一个浏览器 UI。关键是它没有另造第二套 API：浏览器说的就是 `--mode rpc` 那套协议，只是传输从 stdio 换成了 WebSocket。30 个命令原样复用，不支持的 3 个会返回说明原因的错误响应，不会静默挂起。

所以给 pi RPC 写的客户端，改个地址就能连到浏览器这层；反过来，这里验证过的协议行为，对 stdio 也一样成立。下面是团队模式下的真实界面，会话、用量、成员管理、审计都建在同一套协议上。

![浏览器 UI](assets/ui.png)

记三个协议层的坑，都不是写错了，是想当然会出事：

1. 帧层不能用 splitlines()。RPC 是严格 LF 分帧的 JSONL，而 Python 的 `str.splitlines()`（Node 的 `readline` 也一样）会把字符串内部的 U+2028、U+2029 当成换行，一条记录被撕成两半。要用原始字节读取加显式 UTF-8 解码。
2. 出站帧必须用和 stdio 相同的序列化器。传输换成 `emit` 回调之后，如果改用严格编码的 `send_json`，遇到非纯 JSON 的载荷会抛异常，而同步的 `emit` 回调没法把异常传回去，发送任务会静默死掉，表现就是流到一半卡住。
3. 需要用户作答的对话框只发给一个标签页。同一个 confirm 在三个标签页里同时弹出，再等最先作答的那一个，比只问一次更糟。

### 3.2 多租户

加 `--auth-mode team` 就能给团队用，每人一个工作区、一个独立会话，互相看不见；用量按 uid 记账，可以设月度上限；工具调用全程留审计。有三处必须这么做：

- 团队模式下拒绝加载用户扩展。per-uid 的扩展目录，等于给每个同事一个以服务进程权限运行的入口，所以政策扩展改成内置、强制加载、用户关不掉。
- 子进程环境按变量名白名单构造，剔掉 `*_KEY`、`*_TOKEN`、`*_PASSWORD`。共用的 API key 也不能走命令行参数，`ps aux` 对同机所有账号可见。
- `bootstrap` 的 `SystemExit` 要收敛成 `BootstrapError`。它继承自 `BaseException` 而不是 `Exception`，在服务端会直接击穿请求处理。

### 3.3 容器沙盒

白名单加审批不是沙盒。它防的是误操作，防不住恶意。真实边界是容器：设了 `PI_SANDBOX_BACKEND=provisioner` 之后，agent 的每条命令都在一个只挂载该用户工作区的容器里执行，另有一个只挂 docker.sock 的 provisioner 服务负责起停和空闲回收。

另外两条边界也要说清楚。沙盒只覆盖命令执行，read/write/edit 仍然由服务进程直接读写工作区；会话状态在进程内存里，单进程是硬约束，要横向扩展得先换掉注册表。

这句话我写进了文档、代码注释和界面提示。让人觉得安全但其实不安全，比少一个特性更麻烦。

### 3.4 跨语言会话互通

会话格式是继承来的，但让两种语言真的互读是新做的。Python 版能读 pi 写的会话，pi 也能读 Python 版写的。字段名逐个对齐（`stopReason`、`toolCallId`、`cacheRead` 等），undefined 的省略语义一致，还能读 v1/v2，在写回时迁移到 v3。

你可以在终端里用 pi 接着浏览器里的对话往下做，反过来也行。代价是被上游的 schema 绑住，这笔交易是明确的，不免费。

## 四、588 个离线测试

31 个测试文件、588 个测试函数、14.8k 行，全部离线运行，不访问网络，不需要 API key。靠三点：

1. 契约对齐。关键行为是逐字对齐，不是「大致相同」，所以断言可以直接抄上游测试。agent 循环的 12 事件序列就来自上游断言。
2. 黄金用例。会话存储有手工构造的 v3 JSONL 黄金文件，用来验证读取、上下文重建和再序列化，字节保真度还用 node 的 `JSON.stringify(JSON.parse(line))` 交叉验证过。
3. 真实线格式。provider 适配器用 `httpx.MockTransport` 喂固定的 SSE 字节，断言到请求体字段级。不是断言「调用了某个方法」，是断言发出去的 JSON 长什么样。

再往上还有 CLI 端到端：起一个本地假的 OpenAI 兼容服务，用子进程跑真实的 pi-py，从加载配置一路走通流式响应、工具执行到落盘会话。这些都是确定性的，跑一万遍结果一样，CI 里不会有 flaky。

## 五、不做的事和已知的问题

- 没有对真实 provider 做过在线验证。开发环境没有任何 API key，首次接入时请先用小 prompt 验证适配器，别直接上生产。这是最大的验证缺口，不想含糊过去。
- grep 和 find 是纯 Python 实现，不依赖 ripgrep 和 fd，`.gitignore` 的支持也是简化版，语义是接近而非逐字节相同。
- edit 工具没做上游模糊匹配兜底之外的那些启发式。
- 工具越界只由政策扩展拦，`cd /elsewhere && rm -rf *` 这种先改目录再操作的识别不了。

## 六、地址和几点看法

```text
https://github.com/johnson7788/python-pi
```

MIT 协议，和上游一致。仓库里有四层可以独立使用的框架库、一个浏览器 UI、一套 Docker Compose 的团队部署（含沙盒 provisioner），以及 588 个离线测试。

几点看法：

- 如果你在找一个能复用的 agent 底座，可以看看它怎么分层。pi-agent-core 只有 1.8k 行，能绕开编码助手那层单独用。
- 如果你在设计 agent 框架，有两点可以借鉴：partial 这种共享可变对象的取舍，以及把 preflight 和执行分开。配额、白名单、危险命令都在 preflight 判断，用户不用等模型跑完才发现被拒。
- 如果你在给 agent 加多租户，「这不是沙盒」这句话要早点说。把边界讲清楚，比让人误以为安全强。
- 如果 CI 里没有 API key，`httpx.MockTransport` 断言到字段级，加一个假 provider 起子进程跑端到端，这套组合够用。

上游那份代码最值钱的地方，不是它实现了什么，而是它把每个行为的边界都写死了。照着这些边界走，能省下很多时间。

---

## 出处

- 项目仓库（MIT）：<https://github.com/johnson7788/python-pi>
- 上游 pi：<https://pi.dev/docs/latest>
- 架构设计与 16 条易错清单：`docs/ARCHITECTURE.md`
- 与上游的逐条偏差登记：`docs/DEVIATIONS.md`
- 团队版设计与落地复盘：`docs/plans/2026-09-14-team-saas-design.md`
