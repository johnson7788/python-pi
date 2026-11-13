"""Built-in app / work-mode / scene-capsule catalog.

Apps are declarative compositions: a list of work modes, each with a prompt and an
optional skill binding, plus the capsules shown above the composer.  The runtime turns
one mode into a frozen :class:`Composition` when a worker is created.

V1 keeps the catalog in code so every deployment has a usable set out of the box.  The
same shapes can later move to a tenant-editable table without changing the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_APP_ID = "general"
DEFAULT_MODE_ID = "office"


@dataclass(frozen=True)
class Capsule:
    id: str
    name: str
    prompt: str


@dataclass(frozen=True)
class Mode:
    id: str
    name: str
    description: str
    prompt: str
    #: ``None`` means every discovered skill; a tuple binds the mode to a subset.
    skills: tuple[str, ...] | None = None
    capsules: tuple[Capsule, ...] = field(default_factory=tuple)
    #: Example prompts shown on the home screen; empty means "no examples".
    examples: tuple[str, ...] = ()
    #: Composer placeholder for this mode.
    placeholder: str = ""
    #: Icon name from the web icon set (``spark``/``code``/``palette``/...).
    icon: str = ""


@dataclass(frozen=True)
class App:
    id: str
    name: str
    description: str
    accent: str
    modes: tuple[Mode, ...]
    #: Plugins this app needs in order to do its job.  They are still *installed* by an
    #: administrator (a builtin app cannot ship executable code), but an app that declares
    #: them is the deployment's deliberate choice — so they load without a per-user
    #: "enable" click, which is what made a shared app arrive half-working.
    plugins: tuple[str, ...] = ()


GENERAL = App(
    id="general",
    name="通用工作台",
    description="日常办公、开发与设计任务；不限定行业。",
    accent="#3b5fba",
    modes=(
        Mode(
            id="office",
            name="办公",
            description="文档、研究、汇报与日常事务。",
            prompt=(
                "当前应用是「通用工作台 · 办公」。优先给出可直接使用的结论和材料；"
                "涉及事实时标注来源，涉及文件时说明产物路径。"
            ),
            capsules=(
                Capsule("make-ppt", "制作 PPT", "帮我制作一份演示文稿，先给大纲，再逐页生成内容。"),
                Capsule("deep-research", "深度研究", "对这个主题做一次深度研究，给出结论、证据和来源。"),
                Capsule("plan", "策划方案", "帮我策划一个可执行的方案，包含目标、步骤、风险和验收标准。"),
                Capsule("schedule", "定时任务", "帮我设计一个可以定时执行的自动化任务。"),
            ),
            examples=(
                "这个周末，附近有什么值得去",
                "最近 7 天，短视频都在流行什么",
                "每月定投 1000 元，5 年后能有多少钱",
                "最近 AI 行业变了什么，给我一份变更日志",
            ),
            placeholder="/调用技能与指令",
            icon="spark",
        ),
        Mode(
            id="dev",
            name="开发",
            description="读代码、定位问题、补测试、准备发布。",
            prompt=(
                "当前应用是「通用工作台 · 开发」。先读相关代码再下结论；改动要最小，"
                "说明验证方式，并遵守仓库现有风格。"
            ),
            capsules=(
                Capsule("repo-map", "读仓库结构", "先梳理这个仓库的结构、入口和最该关注的文件。"),
                Capsule("bug", "定位一个 bug", "根据我给的报错定位根因，先解释再修复。"),
                Capsule("tests", "补测试", "找出这块逻辑的边界条件并补上测试。"),
                Capsule("release", "准备发布", "检查发布前需要确认的配置、迁移和回滚步骤。"),
            ),
            examples=(
                "把服务端的排队消息改成持久化",
                "这个 traceback 是什么原因，帮我修掉",
                "给会话模块补 5 个边界测试",
                "看看这个仓库最该先补的 3 个能力",
            ),
            placeholder="描述要改的功能，或贴一段报错",
            icon="code",
        ),
        Mode(
            id="design",
            name="设计",
            description="界面、视觉方向与竞品拆解。",
            prompt=(
                "当前应用是「通用工作台 · 设计」。先明确目标用户与使用场景，再给具体界面方案；"
                "视觉判断要可落地，避免空泛形容。"
            ),
            capsules=(
                Capsule("prototype", "做原型", "把这个需求做成一个可点击的 HTML 原型。"),
                Capsule("palette", "配色方案", "给这个产品定一套克制的浅色配色，并说明使用规则。"),
                Capsule("competitor", "竞品分析", "拆解三个同类产品的首屏和关键流程差异。"),
                Capsule("icon", "图标", "为这个功能设计一个简洁的图标方向。"),
            ),
            examples=(
                "把首页做成新版那样，先出 HTML",
                "给这个产品定一套克制的浅色配色",
                "拆解 3 个同类产品的首屏差异",
                "这套界面还缺什么状态，列给我",
            ),
            placeholder="描述你想做的界面或视觉方向",
            icon="palette",
        ),
    ),
)

RESEARCH = App(
    id="research",
    name="研究综述",
    description="循证检索、文献筛选、证据表与综述写作。",
    accent="#2f7d6b",
    modes=(
        Mode(
            id="review",
            name="综述",
            description="从 PICO 到证据表，再到综述初稿。",
            prompt=(
                "当前应用是「研究综述」。按循证流程工作：先明确 PICO 与纳排标准，"
                "再检索和筛选；每个结论都要能追溯到具体文献，不确定的地方明确标注。"
            ),
            capsules=(
                Capsule("pico", "明确 PICO", "先把研究问题拆成 PICO，并给出纳排标准。"),
                Capsule("search", "制定检索式", "为这个 PICO 制定中英文检索式和数据库范围。"),
                Capsule("evidence", "提取证据表", "把纳入研究整理成可追溯的证据表。"),
                Capsule("outline", "综述提纲", "基于现有证据给出综述提纲和每节要点。"),
            ),
            examples=(
                "帮我评估这个临床问题值不值得做系统评价",
                "把这三篇 RCT 整理成证据表",
                "为这个 PICO 写中英文检索式",
                "这份综述初稿的逻辑缺什么",
            ),
            placeholder="输入研究问题、PICO 或文献片段",
            icon="book",
        ),
    ),
)

SLIDES = App(
    id="slides",
    name="演示文稿",
    description="从大纲到逐页内容与讲稿。",
    accent="#b56b2a",
    modes=(
        Mode(
            id="deck",
            name="制作",
            description="先结构，再页面，最后讲稿。",
            prompt=(
                "当前应用是「演示文稿」。先确认受众、时长和核心结论，再给大纲；"
                "每一页只表达一个观点，正文不超过要点式短句。"
            ),
            capsules=(
                Capsule("outline-10", "10 页大纲", "给这个主题做一份 10 页的演示大纲。"),
                Capsule("story", "故事线", "把这份材料改造成有起承转合的汇报故事线。"),
                Capsule("notes", "讲稿", "为这套幻灯片写逐页讲稿，控制在 15 分钟。"),
            ),
            examples=(
                "给季度复盘做一份 10 页汇报",
                "把这份文档改成 15 分钟的汇报故事线",
                "这套幻灯片每页只留一个观点，帮我改",
                "给这页配一段讲稿",
            ),
            placeholder="输入主题、受众和时长",
            icon="slides",
        ),
    ),
)

DRUG = App(
    id="drug",
    name="药品说明书",
    description="按说明书结构查药品：适应症、用法用量、禁忌、相互作用与特殊人群。",
    accent="#8a5a2b",
    modes=(
        Mode(
            id="label",
            name="查说明书",
            description="按说明书结构回答一个药的问题。",
            prompt=(
                "当前应用是「药品说明书」。先确认通用名与剂型（商品名要换算），再按说明书结构回答："
                "适应症、用法用量、禁忌、不良反应、相互作用；肾功能/肝功能/老年/妊娠等特殊人群单列。"
                "写明来源是说明书还是指南；不确定就说不确定，不要编造剂量。"
            ),
            capsules=(
                Capsule("indication", "适应症", "这个药的适应症是什么？说明书里怎么写的？"),
                Capsule("dosage", "用法用量", "常规用法用量、最大剂量和调整原则是什么？"),
                Capsule("contra", "禁忌与不良反应", "有哪些禁忌和需要警惕的不良反应？"),
                Capsule("interaction", "相互作用", "这个药和常见合并用药有哪些相互作用？"),
                Capsule("special", "特殊人群", "老人、儿童、孕期、肝肾功能不全时怎么调整？"),
            ),
            examples=(
                "查一下阿托伐他汀钙片的说明书",
                "肾功能不全时这个药要减量吗",
                "华法林和哪些药不能一起用",
                "这个药的常见不良反应有哪些",
            ),
            placeholder="输入药品名，或要核对的用药问题",
            icon="file",
        ),
        Mode(
            id="compare",
            name="多药核对",
            description="对比多种药物的适应症、剂量与相互作用。",
            prompt=(
                "当前应用是「用药核对」。对多种药物逐项对比适应症、剂量、代谢途径与相互作用，"
                "指出重复成分和需要监测的指标；结论用表格，风险点单独列出。"
            ),
            capsules=(
                Capsule("table", "对比表", "把这几种药的适应症、剂量和注意事项做成对比表。"),
                Capsule("duplicate", "重复用药", "这几种药里有没有重复成分或同类叠加？"),
                Capsule("monitor", "监测指标", "联合用药期间需要监测哪些指标、多久一次？"),
            ),
            examples=(
                "这五种降压药帮我做个对比",
                "患者同时在吃这几种药，有没有重复？",
                "联合用药需要查什么指标",
            ),
            placeholder="贴药品清单，或描述联合用药方案",
            icon="sheet",
        ),
        Mode(
            id="safety",
            name="用药安全",
            description="禁忌、相互作用、过量与监测。",
            prompt=(
                "当前应用是「用药安全」。围绕禁忌、相互作用、过量与监测给出可执行建议；"
                "先给结论，再给依据；涉及具体患者时提示需要医生确认。"
            ),
            capsules=(
                Capsule("risk", "风险点", "这个用药方案最大的 3 个风险是什么？"),
                Capsule("overdose", "过量处理", "如果过量，应该怎么处理、观察什么？"),
                Capsule("elderly", "老年用药", "老年人用这个药要注意什么？"),
            ),
            examples=(
                "这个方案对 80 岁老人有什么风险",
                "用药过量应该观察哪些表现",
                "长期吃这个药需要定期查什么",
            ),
            placeholder="描述患者情况和用药方案",
            icon="skill",
        ),
    ),
)

STATS = App(
    id="stats",
    name="临床统计",
    description="研究设计、样本量、统计方法与结果写作。",
    accent="#2f6f8f",
    modes=(
        Mode(
            id="design",
            name="方案设计",
            description="研究问题、终点、样本量与偏倚控制。",
            prompt=(
                "当前应用是「临床统计 · 方案设计」。先明确研究问题、终点、比较组与样本量假设；"
                "给出可执行的样本量估算和偏倚控制清单；涉及公式时写清每个参数。"
            ),
            capsules=(
                Capsule("sample", "样本量", "帮我估算这个研究的样本量，说明假设。"),
                Capsule("endpoint", "终点选择", "主要终点和次要终点应该怎么定？"),
                Capsule("bias", "偏倚控制", "这个设计最容易出现哪些偏倚，怎么控制？"),
            ),
            examples=(
                "两组率的比较需要多大样本量",
                "这个回顾性研究的主要终点怎么定",
                "怎么避免选择偏倚",
            ),
            placeholder="描述研究问题、分组和预期效应",
            icon="sheet",
        ),
        Mode(
            id="analyze",
            name="统计分析",
            description="选方法、查假设、读结果。",
            prompt=(
                "当前应用是「临床统计 · 分析」。先检查数据类型、分布与缺失，再选择检验方法并说明假设；"
                "给出可复现的步骤和结果解读，不要只给 p 值。"
            ),
            capsules=(
                Capsule("test", "选检验", "这组数据该用什么检验？为什么？"),
                Capsule("assumption", "前提检查", "用这个方法前要检查哪些前提？"),
                Capsule("interpret", "结果解读", "帮我解读这组结果，别只念 p 值。"),
            ),
            examples=(
                "这组数据该用 t 检验还是秩和检验",
                "重复测量数据怎么分析",
                "这个回归结果怎么解读",
            ),
            placeholder="描述数据形态，或贴统计输出",
            icon="code",
        ),
        Mode(
            id="write",
            name="结果写作",
            description="方法段、结果段、图表说明。",
            prompt=(
                "当前应用是「临床统计 · 写作」。按期刊方法/结果段的结构写：设计、对象、终点、统计方法、"
                "结果数值（含置信区间）；不夸大因果，不省略阴性结果。"
            ),
            capsules=(
                Capsule("methods", "方法段", "把这个统计方法写成期刊的方法段。"),
                Capsule("results", "结果段", "把结果写成一段，含效应量和置信区间。"),
                Capsule("table", "三线表", "把结果整理成三线表，并写表注。"),
            ),
            examples=(
                "帮我把方法段写清楚",
                "结果段怎么写才不夸大",
                "把这张表改成三线表",
            ),
            placeholder="贴结果或描述分析，我来组织成文字",
            icon="file",
        ),
    ),
)

GRANT = App(
    id="grant",
    name="标书与项目",
    description="立项、投标、评审自查与材料组织。",
    accent="#7a4b9e",
    modes=(
        Mode(
            id="proposal",
            name="立项",
            description="背景、问题、目标、创新点、路线。",
            prompt=(
                "当前应用是「标书与项目 · 立项」。按背景、科学问题、目标、创新点、技术路线、可行性、"
                "预期成果组织；每个创新点都要能被验证，避免空泛形容词。"
            ),
            capsules=(
                Capsule("background", "立项依据", "帮我写立项依据，要有数据和缺口。"),
                Capsule("innovation", "创新点", "提炼 3 个能被验证的创新点。"),
                Capsule("route", "技术路线", "把技术路线画成可执行的步骤。"),
            ),
            examples=(
                "帮我写一份立项依据",
                "这个项目的创新点怎么提炼",
                "技术路线怎么拆成里程碑",
            ),
            placeholder="描述项目方向、已有基础和约束",
            icon="folder",
        ),
        Mode(
            id="bid",
            name="投标",
            description="对照评分点组织技术方案。",
            prompt=(
                "当前应用是「标书与项目 · 投标」。对照评分点组织技术方案，逐条回应需求与偏离；"
                "不确定的资质、案例和参数标注待补，不要编造。"
            ),
            capsules=(
                Capsule("points", "评分点", "把招标文件拆成评分点清单。"),
                Capsule("response", "逐条响应", "按评分点写技术响应。"),
                Capsule("deviation", "偏离表", "整理正偏离/负偏离表。"),
            ),
            examples=(
                "把这份招标要求拆成评分点",
                "帮我写技术响应",
                "哪些地方可能被判负偏离",
            ),
            placeholder="贴招标要求或评分办法",
            icon="file",
        ),
        Mode(
            id="review",
            name="评审",
            description="以评审专家视角挑问题。",
            prompt=(
                "当前应用是「标书与项目 · 评审」。以评审专家视角挑问题：创新性、可行性、预算合理性、"
                "团队匹配度；按严重程度排序并给具体修改建议，不说客套话。"
            ),
            capsules=(
                Capsule("mock", "模拟评审", "按评审专家视角给这份材料打分并挑毛病。"),
                Capsule("budget", "预算合理性", "这个预算哪里不合理？"),
                Capsule("team", "团队匹配", "团队和任务匹配吗？缺什么角色？"),
            ),
            examples=(
                "模拟评审这份立项书",
                "这个预算会被质疑吗",
                "团队配置有什么短板",
            ),
            placeholder="贴材料摘要或预算表",
            icon="search",
        ),
    ),
)

MARKET = App(
    id="market",
    name="市场情报",
    description="竞品拆解、政策跟踪与一页简报。",
    accent="#b56b2a",
    modes=(
        Mode(
            id="competitor",
            name="竞品",
            description="定位、能力、证据与商业模式。",
            prompt=(
                "当前应用是「市场情报 · 竞品」。按定位、目标客户、核心能力、证据等级、商业模式拆解，"
                "给可比较的表格和差异结论；区分事实与推测，推测要标明。"
            ),
            capsules=(
                Capsule("matrix", "竞品矩阵", "把这几家竞品做成对比矩阵。"),
                Capsule("moat", "差异与壁垒", "他们的差异化和壁垒在哪？"),
                Capsule("evidence", "证据等级", "这些宣称有多少证据支撑？"),
            ),
            examples=(
                "对比这三家医学 AI 公司的产品",
                "竞品的壁垒到底在哪",
                "这个宣称有证据吗",
            ),
            placeholder="输入竞品名、产品页或材料",
            icon="search",
        ),
        Mode(
            id="policy",
            name="政策",
            description="要点、生效时间、影响与动作。",
            prompt=(
                "当前应用是「市场情报 · 政策」。先给政策要点与生效时间，再分析对本产品/客户的影响"
                "和可执行动作；引用原文条款，区分强制要求与鼓励方向。"
            ),
            capsules=(
                Capsule("summary", "政策要点", "把这份政策压缩成 5 条要点。"),
                Capsule("impact", "影响分析", "对我们产品和客户有什么影响？"),
                Capsule("action", "行动清单", "接下来 30 天该做什么？"),
            ),
            examples=(
                "解读这份医保/数据政策",
                "新规对我们产品有什么影响",
                "列出合规动作清单",
            ),
            placeholder="贴政策原文或链接摘要",
            icon="book",
        ),
        Mode(
            id="brief",
            name="简报",
            description="一周信息压缩成一页。",
            prompt=(
                "当前应用是「市场情报 · 简报」。把一周信息压缩成一页：三件最重要的事、影响、建议动作；"
                "每条带来源与日期，没有来源的信息标注待核实。"
            ),
            capsules=(
                Capsule("weekly", "一周简报", "把这周的信息做成一页简报。"),
                Capsule("signal", "信号与噪声", "哪些是真正值得跟的信号？"),
                Capsule("action", "建议动作", "基于这些信息，建议做什么？"),
            ),
            examples=(
                "把这周的行业新闻做成一页简报",
                "哪些信息值得继续跟",
                "给管理层写三条建议",
            ),
            placeholder="贴一周的新闻、纪要或链接",
            icon="slides",
        ),
    ),
)

PATIENT = App(
    id="patient-edu",
    name="患者教育",
    description="科普文章、患者问答与一页患教材料。",
    accent="#2f7d6b",
    modes=(
        Mode(
            id="article",
            name="科普文章",
            description="用患者能懂的语言讲清楚。",
            prompt=(
                "当前应用是「患者教育 · 科普」。用初中文化能读懂的语言解释：是什么、为什么、怎么办、"
                "什么时候必须就医；不用恐吓式表达，不替代医生诊断，不推荐具体商品。"
            ),
            capsules=(
                Capsule("explain", "讲清楚一个病", "用大白话解释这个病是怎么回事。"),
                Capsule("myth", "常见误区", "关于这个病最常见的 3 个误区是什么？"),
                Capsule("visit", "就医信号", "出现哪些情况必须马上就医？"),
            ),
            examples=(
                "用大白话讲讲高血压",
                "关于糖尿病足有哪些误区",
                "什么情况下必须去急诊",
            ),
            placeholder="输入疾病、检查或治疗主题",
            icon="house",
        ),
        Mode(
            id="qa",
            name="患者问答",
            description="一句结论 + 三条建议 + 警示信号。",
            prompt=(
                "当前应用是「患者教育 · 问答」。回答先给一句结论，再给 3 条以内可执行建议；"
                "列出需要立即就医的警示信号；不猜测检查结果，不调整处方。"
            ),
            capsules=(
                Capsule("answer", "回答一个问题", "患者问：这个药要吃多久？"),
                Capsule("warning", "警示信号", "这种情况要不要马上去医院？"),
                Capsule("followup", "复诊提醒", "复诊前需要准备什么？"),
            ),
            examples=(
                "患者问漏服一次药要不要补",
                "术后多久能恢复正常活动",
                "复查前需要空腹吗",
            ),
            placeholder="输入患者原话或问题",
            icon="skill",
        ),
        Mode(
            id="material",
            name="患教材料",
            description="一页纸：要点、用药、复诊。",
            prompt=(
                "当前应用是「患者教育 · 患教材料」。输出一页纸：要点、用药交代、生活注意事项、"
                "复诊时间、联系方式占位；控制在一屏内，句子短，动词开头。"
            ),
            capsules=(
                Capsule("handout", "一页患教", "把这个病的患教材料做成一页纸。"),
                Capsule("medication", "用药交代", "出院带药的交代怎么写清楚？"),
                Capsule("checklist", "出院清单", "出院前患者要确认哪些事？"),
            ),
            examples=(
                "做一份高血压患者的一页患教",
                "出院带药怎么交代",
                "写一份术后复查清单",
            ),
            placeholder="输入病种、治疗或出院场景",
            icon="file",
        ),
    ),
)

CHART = App(
    id="chart",
    name="数据绘图",
    description="数据 → 图表：ECharts 交互图与仪表盘，可导出 PNG/CSV；图里的每个数字都能回到数据行。",
    accent="#b45309",
    plugins=("chart-tools",),
    modes=(
        Mode(
            id="single",
            name="单图",
            description="一句话或一份表 → 一张能交互的图 + 一句结论。",
            prompt=(
                "当前应用是「数据绘图 · 单图」。顺序固定：①chart_data_profile 看真实结构；"
                "②用 chart_templates 取最接近的模板（时间→line-basic，分类→bar-basic/bar-horizontal，"
                "≤6 类占比→pie-basic，两个数值→scatter；不要凭记忆写 option）；"
                "③改 bind.path 和标题，存成 charts/<名字>.spec.json，先 chart_check 再 chart_render_html；"
                "④present 生成的 html 和 spec。"
                "数字一律由 bind 从数据文件绑定：spec 里不许写 dataset.source，也不许写 series.data 的"
                "数字字面量；encode 里引用的列必须在 bind.columns 里。"
                "只画一张图就够的时候不要拼仪表盘；图回答不了的问题用一句话说清。"
            ),
            skills=("chart-spec", "chart-gallery"),
            capsules=(
                Capsule("trend", "看趋势", "把这份数据画成趋势图，标出拐点和异常点。"),
                Capsule("compare", "做对比", "按分类对比这几组数值，选最不容易误读的图型。"),
                Capsule("relation", "看关系", "看看这两个指标有没有关系，用散点并说明相关不等于因果。"),
                Capsule("share", "改成能发的图", "把这张图改成适合放进汇报/邮件的样式，字号和图注都清楚。"),
            ),
            examples=(
                "把 data/ 里的销售表画成月度趋势图",
                "各地区销售额做成对比图，最多 6 类",
                "这两个指标的关系画出来，顺便告诉我相关性强不强",
                "把这份 CSV 画成一张可以直接放进周报的图",
            ),
            placeholder="上传数据，或说清楚要对比/看趋势的字段",
            icon="chart",
        ),
        Mode(
            id="dashboard",
            name="仪表盘",
            description="一个数据集出 3–6 张图：一屏看清结论、构成和异常。",
            prompt=(
                "当前应用是「数据绘图 · 仪表盘」。一个 page 里放 3–6 张图（page.columns 2 或 3），"
                "每张图只回答一个问题，标题写结论而不是字段名（如「华东贡献了六成增长」而不是「sales by region」）。"
                "先 chart_data_profile，再定三件事：总量趋势、构成占比、异常/对比；同一页里不要出现两张"
                "表达同一件事的图。每张图的 option 用 chart_templates 取对应模板，所有图共用一份 bind；"
                "spec 里不写数据。写完先 chart_check，再用 chart_render_html 生成 HTML，最后 present html 和 spec。"
                "生成后按「结论 → 支撑图 → 边界」在对话里给出一屏能读完的摘要。"
            ),
            skills=("chart-spec", "chart-gallery"),
            capsules=(
                Capsule("overview", "一页总览", "用这份数据做一个一页总览：趋势、构成、异常各一张。"),
                Capsule("layout", "排布局", "把这几张图排成一屏，按重要性决定大小和顺序。"),
                Capsule("kpi", "加关键指标", "在仪表盘顶部加 3 个关键指标，并说明口径。"),
            ),
            examples=(
                "用这份销售数据做一个一页经营看板",
                "把这份月度数据做成 4 张图的一屏总览",
                "给我一个能放进周会的一页图",
            ),
            placeholder="上传数据，说明这一屏要回答什么问题",
            icon="chart",
        ),
        Mode(
            id="insight",
            name="数据解读",
            description="先看数据，再给图：三条发现、一个反例、以及不能下的结论。",
            prompt=(
                "当前应用是「数据绘图 · 数据解读」。先 chart_data_profile，再回答三件事："
                "①数据里最明显的三个模式（每个都要有数字和出处列）；②一个反例或异常（缺失、离群、口径变化）；"
                "③这些数据**不能**支持什么结论。图只用来支撑发现，最多三张，不要为了好看加图。"
                "相关不等于因果，趋势不等于预测，样本量小要直接说 n。"
                "图用 chart_templates 取模板、chart_render_html 生成；报告写成 output/<名字>.md，"
                "最后 present 图和报告。"
            ),
            skills=("chart-spec",),
            capsules=(
                Capsule("findings", "三条发现", "先看数据，再给三条最值得说的发现，每条带数字。"),
                Capsule("anomaly", "找异常", "这份数据里有没有异常值、缺失或口径不一致？会不会改变结论？"),
                Capsule("caveat", "能说到什么程度", "这份数据能支持哪些结论、不能支持哪些？逐条说。"),
            ),
            examples=(
                "这份数据说明了什么，给我三条发现",
                "有没有异常值会影响结论",
                "这些数据能不能支持「投放带来了增长」",
            ),
            placeholder="上传数据，说明你想弄清楚的业务问题",
            icon="chart",
        ),
        Mode(
            id="restyle",
            name="图表改版",
            description="把现有的图（截图或旧 HTML）改成更清楚、更克制、更适合汇报的版本。",
            prompt=(
                "当前应用是「数据绘图 · 图表改版」。先确认原图要表达什么、哪里在误导（断轴、双轴、"
                "3D、饼图分类过多、缺少单位和 n），再决定改类型还是只改样式。"
                "如果用户给的是截图，先把能用到的数字/类目落到工作区的数据文件，用 chart_data_profile "
                "核对结构，再 bind；不要凭截图估算数字。"
                "新图用 chart_templates 取模板，写成 charts/<名字>.spec.json，用 chart_render_html 生成，"
                "不要手写 HTML。"
                "改完列出「改了什么、为什么、原图哪里有问题」，不要只交一张新图。"
            ),
            skills=("chart-spec", "chart-gallery"),
            capsules=(
                Capsule("type", "换图型", "这张图的类型不合适，换成更不容易误读的，并说明理由。"),
                Capsule("style", "调样式", "颜色、字号、网格、图注按汇报场景重做，不要花。"),
                Capsule("mislead", "纠正误导", "找出这张图会让人误读的地方，逐条改掉。"),
            ),
            examples=(
                "这张图哪里容易误读，帮我改掉",
                "把这个截图里的图重画成能放进汇报的版本",
                "这个双轴图看着有问题，换成更清楚的表达",
            ),
            placeholder="贴截图，或给出要改的 HTML/spec 路径",
            icon="palette",
        ),
    ),
)

APPS: tuple[App, ...] = (
    GENERAL,
    CHART,
    RESEARCH,
    SLIDES,
    DRUG,
    STATS,
    GRANT,
    MARKET,
    PATIENT,
)
_BY_ID = {app.id: app for app in APPS}


def find_app(app_id: str | None) -> App | None:
    return _BY_ID.get(app_id or DEFAULT_APP_ID)


def find_mode(app_id: str | None, mode_id: str | None) -> tuple[App, Mode] | None:
    app = find_app(app_id)
    if app is None:
        return None
    wanted = mode_id or app.modes[0].id
    for mode in app.modes:
        if mode.id == wanted:
            return app, mode
    return None


def composition_for(app_id: str | None, mode_id: str | None) -> tuple[str, tuple[str, ...] | None]:
    """Return ``(prompt, skill_allowlist)`` for a validated app/mode pair."""
    found = find_mode(app_id, mode_id)
    if found is None:
        found = find_mode(DEFAULT_APP_ID, DEFAULT_MODE_ID)
        if found is None:  # pragma: no cover - the catalog always has a default
            return "", None
    _app, mode = found
    return mode.prompt, mode.skills


def app_json(app: App) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "description": app.description,
        "accent": app.accent,
        "modes": [
            {
                "id": mode.id,
                "name": mode.name,
                "description": mode.description,
                "skillBinding": "all" if mode.skills is None else list(mode.skills),
                "prompt": mode.prompt,
                "examples": list(mode.examples),
                "placeholder": mode.placeholder,
                "icon": mode.icon,
                "capsules": [
                    {"id": capsule.id, "name": capsule.name, "prompt": capsule.prompt}
                    for capsule in mode.capsules
                ],
            }
            for mode in app.modes
        ],
    }


def catalog_json() -> dict[str, Any]:
    return {
        "apps": [{**app_json(app), "builtin": True} for app in APPS],
        "defaultApp": DEFAULT_APP_ID,
        "defaultMode": DEFAULT_MODE_ID,
    }
