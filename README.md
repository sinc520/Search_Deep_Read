# search-deep-read

> 对单次搜索结果做深度解读：主轨读 10 条，子智能体读全 50 条，只回增量。
>
> 本文是设计与选型说明；运行入口见 `SKILL.md`。

**花名**：本来想叫 `deepseek-deepsearch-deepread`（三个 deep 连读，像绕口令），被 AI 拦住了——理由是这名字会把引擎焊死在标题上，而且技能列表里会被截断。所以只在这里留个纪念。

## 0. 快速开始

### 0.1 它针对什么（先说边界）

**一个窄需求**：在 **WorkBuddy** 里调用 **DeepSeek 搜索 API** 检索之后，把**这一次**返回的 50 条结果读透。

**它的典型用途是地毯式搜索**——不追求"搜得快"，而是尽量不错过散落在各个角落里、零散但重要的信息与线索。

之所以说"窄"，是因为它建立在三个特定条件上：

1. 检索层单次能拿 50 条，且**按请求计费**——请求 50 条和请求 5 条同价，所以多取不额外花钱。这是整个设计的起点，换一个按条计费的 API 就不成立；
2. 每条结果带一段 1000–5000 字的原文摘录，全部读进上下文会直接撑爆窗口；
3. 运行在支持**子智能体**（独立上下文、只回传摘要）的 agent 框架里，比如 WorkBuddy。

核心流程（存档 → 主轨读前 10 → 子智能体读全量 → 定向 expand）**本身与平台无关**，理论上可以搬到别的搜索 API、别的 Agent 框架。但**目前只在这个场景下验证过**，别处不做承诺。

它**不解决"该搜什么"**——那是多关键词扇出的事，判断标准见后面「用不用它：先问一句」。

### 0.2 装和跑

**依赖**：Python 3.10+，零第三方库。需要一个 DeepSeek API key（[platform.deepseek.com](https://platform.deepseek.com/)）。

```bash
# 给 key，三选一
export DEEPSEEK_API_KEY=sk-xxx        # 推荐
#   --api-key sk-xxx
#   或写进 ~/.workbuddy/models.json（WorkBuddy 用户已有这条）

# 跑一次
python scripts/search.py "某研究主题 关键词A 关键词B" --count 20

# 看这次搜索的分布（离线，0 API 成本）
python scripts/dump.py <上一步输出里的存档路径> --stats
```

**装成技能**：把整个目录放进 agent 框架约定的技能目录即可——

- WorkBuddy：`~/.workbuddy/skills/search-deep-read/`
- 其它支持 `SKILL.md` 的框架：放到它自己的 skills 目录

`SKILL.md` 是运行时入口（什么时候该用、什么时候不该用、怎么派子智能体）；本 README 只在人读的时候有用，不会被加载进上下文。

**存档落在哪**：默认写到「当前项目」的 `.workbuddy/search-results/<日期>/`，不跟随技能目录；`--archive-scope global` 切到用户级，`--archive-dir` 任意指定。

## 1. 为什么有这个技能

起因是一个具体问题：内置的 WebSearch（腾讯元宝）单次只回 5 条，服务端硬顶 10 条，且引擎不可切换。

进一步实测发现，条数只是表象，真正的瓶颈是**来源质量**。

## 2. 实测证据（2026-11-03，同机同日同查询）

### 2.1 同一查询的横向对比

查询：`retrieval augmented generation how many retrieved documents optimal`

| | 内置 WebSearch（元宝） | DeepSeek Search |
|---|---|---|
| 一手源占比 | **0 / 10** | **8 / 10** |
| 来源分布 | 全为中文博客 / CSDN 系 | ACM DL、Hugging Face Papers、Springer、Harvard、Observatoire de Paris、GitHub |
| 关键差异 | 未命中目标论文 | 第 1 条即 TOSEM 论文（此前只能绕道 arXiv API 手动抓取） |

### 2.2 中文专业查询

查询：`某研究主题 N 个关键词 最新进展`（中文）

DeepSeek 返回 10 条，`authority` 全为 4：DigitalCommons@TMC、ScienceDirect ×2、NIH ×3、Elsevier、Nature、FEBS Press、국립의과학지식센터。

也就是说，自建英文索引对**中文专业查询**同样能召回一手文献，这一点超出预期。

### 2.3 条数上限

- 请求 `count=30` → 实际返回 **30 条**，耗时 0.57s；`count` 上限 50。
- 对照：向内置 WebSearch 请求 20 条 → 服务端只回 **10 条**（上限在服务端，客户端改不动）。

### 2.4 双引擎重合度：同一问题，元宝与 DeepSeek 各给了什么（2026-11-06 补测）

**为什么要测**：文档里一直只有定性说法（「元宝强中文、DeepSeek 强学术」），但从没量过两者到底重叠多少——而这个数字直接决定「双引擎并行」值不值得做。

**方法**：取 3 对**同一天、同一关键词**的存档（各 1 份元宝 10 条 + 1 份 DeepSeek 50 条），URL 归一化（去协议/参数/锚点/语言段）后比对；另算 host 重合与标题相似（Jaccard ≥ 0.34 记为「主题相同、页面不同」）。

| 对比对 | URL 完全相同 | 同 host | 标题高度相似 |
|---|---|---|---|
| 英文「搜索 API 排行」 | **1 / 10** | 3 / 10 | **8 / 10** |
| 中文「国内引擎」 | **0 / 10** | 1 / 10（CSDN） | **1 / 10** |
| 英文「独立索引」 | **0 / 10** | 1 / 10 | 5 / 10 |

30 条元宝结果对 150 条 DeepSeek 结果，**只有 1 个 URL 完全重合**。

**三个结论**：

1. **页面几乎不重叠，但内容可以高度重叠。** 英文排行那组，两边都在给「2026 最佳 AI 搜索 API」的 listicle（标题相似度最高一对 Jaccard = 1.00），只是每篇发在不同站。所以「不重叠」说的是**落在不同页面**，不是「信息不同」。
2. **中文侧才是真分野。** 中文那组两边连站群都不挨着：元宝给的是内容农场/SEO 站（shrzkj、aitool-hub、miaowageo），DeepSeek 给的是腾讯云文档、微信公众号、B 站。像在两个互联网里搜。
3. **判重必须看内容，不能只看 URL。** 上面第 1 点直接推出：靠 URL 去重几乎无效（`merge.py` 实测 150 条只去掉 2 条），真正的重复是「同一批数字被不同站复述」——只有读过内容才发现得了。

**限定（别把这组数字当普适规律）**：

- 样本只有 3 对，且都是「工具 / 方案 / 排行」类查询——这类内容 listicle 极多，天然分散；搜**新闻事件、公司动态**时前几条大概率高度重合。
- 元宝 10 条 vs DeepSeek 50 条，量不对等。不过另做了对等比较（元宝 10 条 vs DeepSeek **前 10 条**），重合仍是 0–1，说明低重合不是取样深度造成的。
- **低重合 ≠ 两边都好**。中文那组元宝侧大半是内容农场，DeepSeek 侧也有偏题条目。重合度只说明来源不同，不说明质量。

**方法论坑（复现时注意）**：不要用 `site_name` 跨引擎比域名——DeepSeek 的 `site_name` 是展示名（`ScienceDirect.com`），元宝的是域名（`www.xxx.cn`），直接比会得到「三对全 0」的假结果。两者都从 `url` 推 host 才是同一个口径。

### 2.5 为什么列表用 1000 字而不是 180（从 SKILL.md 下沉）

| 来源 | 平均信息句 | 平均长度 |
|---|---|---|
| `summary` 前 180 字 | **0.05 句** | 180 |
| `snippet` | 0.33 句 | 428 |
| `summary` 全文 | **2.14 句** | ~2600 |

（63 条样本，按"含数字/结论的句子"计。180 字几乎装不下一句完整的高信息句。）

档位依据：元宝每条 snippet 中位 1043 字、DeepSeek snippet 中位 330 字、summary 中位 2511 字。
`default` 的 1000 字正好对齐元宝的实际体验，`brief` 是 DeepSeek 原生 snippet 的原始密度，`deep` 不截断。

## 3. 设计决策

### 3.0 为什么不按 authority 预过滤（2026-11-04 改）

原设计默认 `--min-authority 3`，理由是"滤掉低质源"。实测发现这个理由是错的：

- **顺序本身就是排序**。引擎返回的 `results` 严格按 `rerank_score` 降序（实测 50 条单调不增），而 `rerank_score` 是相关度+权威+时效的合成分。再按 authority 切一刀，等于在引擎已经排好的序列上做二次筛选。
- **authority 衡量的是域名眼熟程度，不是内容质量**。首跑那次被它判为 2（从而被滤掉）的 17 条里，包含：一份 Agent 设计模式白皮书（#29）、Awesome Agentic Patterns 的模式条目（#40）、一篇讲"何时派子代理"的实战文（#36，含 7 条派发信号）、以及一个标题抓取成 "Page Not Found" 但正文完整的好页面（#37）。同时保留下来的 33 条里，也有自动生成的空壳页。
- **对主轨毫无收益**：那一例的前 10 条本来就全是 authority=3，过滤前后完全一样；它唯一的作用是把子轨的样本从 50 砍到 33，直接违背"子轨读全量"的承诺。

**新策略：主轨按引擎顺序读前 N 条，子轨读全部 50 条，都不过滤。** `--min-authority` 保留为可选开关（真要排除某类源时用），一旦用了会写进存档的 `filters`，replay 与 dump 都据此复现同一套口径。

### 3.1 为什么是脚本，不是 MCP

- **参数完整度**：MCP 工具的入参完全由 server 的 `inputSchema` 决定。现成包装普遍只暴露 `query`，把 `count`/算子/`need_content` 砍掉。自写脚本可 100% 暴露。
- **上下文成本**：MCP 结果按 wrapper 格式化后整段回灌；脚本可在返回前做 URL 去重、摘要截断、按档位控制展示条数。
- **故障面**：MCP 多一层进程（npx 拉包 / Node 版本 / 启动失败）。脚本是一个 HTTP 请求。
- **存档友好**：脚本能在任何终端直接跑，不依赖客户端是否信任该 MCP。

### 3.2 为什么输出只走纯文本

经代码核查，WorkBuddy 客户端：

- `outputSchema` 出现次数 **0** —— 不支持 MCP 规范里的工具结构化输出声明；
- `structuredContent` 的 13 处全部服务于 MCP-UI（iframe 渲染、`updateModelContext`），不在工具结果通道；
- 工具结果经 `convertMcpResult` 只保留 `content` 的 text。

结论：即使写 MCP，结构化字段最终仍以字符串进入模型上下文。因此本技能**只输出规整文本**（一行一条，`[authority|rerank_score]` 前缀），不返回 `structuredContent` —— 后者还会因 mcp-ui 路径被再 stringify 一次，导致同一份数据重复进上下文。

### 3.3 为什么站点/时间过滤走查询算子

官方文档明确支持 `site:`、`-site:`、`after:`、`before:`、`lang:`、精确短语与排除词。社区文章中出现的 `freshness` 参数**不在官方 Search 接口文档内**（那是博查的字段），因此本脚本不臆造该参数，一律拼成算子。

参数以外，脚本自加了 `--min-authority`、`--sort`、`--dedup` 三个**本地**能力，因为它们用服务端已有的字段就能算，无需额外请求。

### 3.4 产物落盘：存什么、不存什么

**问题**：工具结果默认只进上下文。而 CLI 有 microcompact 机制，会定期清理旧的工具结果内容（代码里可见 `[Old tool result content cleared by microcompact]` 标记）。结果是：搜索产物在长会话中**会被主动丢弃**，重复查询要重复付费，也无法回溯"上次那条结果的 authority 是几"。

**决策**：每次搜索默认落盘原始响应，`--no-save` 可关闭。

| 内容 | 是否入库 | 理由 |
|---|---|---|
| 完整 `summary`（未截断） | **存** | 数据已经付过费，不存等于白花；实测单条最长 4502 字符 |
| `snippet` / `authority` / `rerank_score` / `date` / `url` | 存 | 后续重排、筛选、溯源全靠这些字段 |
| 请求参数、耗时、原始 query 与算子展开后的 query | 存 | 复盘"当时怎么问的、为什么是这个结果" |
| 网页正文 `content` | **不存** | 需 `need_content=true` 才触发，单页可达数万字；属另一条链路，要正文时单独取并另存 |

**组织方式**：**每次搜索一个独立 JSON 文件**，外加一份全局 `index.jsonl`。不采用"单文件追加"。

```
<项目>/.workbuddy/search-results/      # 默认 project 作用域
├── index.jsonl
└── 2026-11-03/
    ├── your-topic-keywords-103838-4fc4.json
    └── q-7c1e2b9d-104203-55e1.json
```

为什么分文件而不是单文件追加：

| 方案 | 问题 |
|---|---|
| 单文件追加（JSONL） | 文件无限增长、无法按需只加载某一次、删单条要重写整个文件、并发写有损坏风险 |
| **分文件 + 轻索引** | 可单独读/删/diff/`--replay`；索引只有元数据（一行约 200 字节），grep 即可定位 |

作用域默认跟随项目（跨项目通用知识用 `--archive-scope global`），解析优先级：`--archive-dir` > `DEEPSEEK_SEARCH_ARCHIVE` > `CODEBUDDY_PROJECT_DIR` 下 `.workbuddy/search-results` > 当前目录 > 全局目录。

`index.jsonl` 是关键：定位一次历史搜索只需 grep 这一行文件，不必遍历目录。文件名用 ASCII slug + 时分秒 + 参数哈希；全中文查询 slug 为空时退化为 `q-<sha1>`，避免中文文件名在各 shell 下的编码问题。

**读取纪律**（写进 SKILL.md）：先查 index，用 `--replay` 复用旧结果，**不要整文件读进上下文**。一次 6 条结果的存档约 22 KB，整读相当于把当初省下的 token 又花一遍。

### 3.5 元宝的结果也存档（`save_yuanbao.py`，2026-11-06 加）

内置 WebSearch（腾讯元宝）的结果**只进上下文、不落盘**：长会话里工具结果会被 microcompact 清理，跨会话更看不到。于是「上周那个查询元宝给了哪几条」只能翻会话记录考古，也没法和 DeepSeek 那批放在一起比对。

`save_yuanbao.py` 把会话记录里的那批结果转成**与 `search.py` 同构的存档**，写进同一目录、同一个 `index.jsonl`（多一个 `engine: "yuanbao"` 字段），所以 `dump.py` / `merge.py` / `--replay` **零改动**就能读。

```bash
python scripts/save_yuanbao.py            # 存最近一次 WebSearch
python scripts/save_yuanbao.py --list 10  # 先看有哪些（带「已存/未存」标记）
python scripts/save_yuanbao.py --pick 3   # 存倒数第 3 次，可回溯历史查询
python scripts/save_yuanbao.py --print    # 只解析、不写盘
```

**上下文成本为零**：脚本读的是磁盘上已有的会话记录（`type=function_call_result`、`name=WebSearch`、`output.text`），不需要把结果文本再输出一遍。若改成「让模型转写一遍」，一次 2–4 万字符等于把存档的代价翻倍。

**字段差异（不要硬套 DeepSeek 那套语义）**：

| 字段 | DeepSeek | 元宝 |
|---|---|---|
| `snippet` | 100–400 字，查询相关的中段切片 | **就是唯一一档正文**，实测 647–6000 字 |
| `summary` / `content` | 有，600–4500 字 / 需 `need_content` | **没有**，留空 |
| `authority` / `rerank_score` | 0–4 / 0–3 | **没有**，写 `null`，展示为 `—` |
| `date` | 条目日期 | 不提供，留空；检索日期记在顶层 `search_date` |
| 排名 | 引擎返回顺序（rerank 降序） | 同左，另存 `rank` |

**去重**：按 `call_id`。同一批结果重复跑不会写第二份，要覆盖加 `--force`。

**风险**：这条链路依赖会话记录的内部格式。客户端升级后若失效，脚本会明确报「会话格式可能已变」并提示 `--from-file` 手工兜底（把结果文本存成文件再喂给它），而不是静默写出 0 条。

**什么时候该存**：同一问题要做双引擎对照、结果要进产物、或要跨会话复用时。日常扫一眼不存——否则 `search-results/` 会堆满一次性东西，`grep index.jsonl` 反而更难找。

### 3.6 作为模块接入：接在哪里、怎么接、以及它不做什么（2026-11-06 加）

**接入点只有一个**：任何流程里「搜索刚刚返回结果」的那一刻。本 skill 只负责把**这一批**读透；宿主的扇数、轮次、输出格式，一概不动。

#### 三条硬约束（写在最前面，是为了防止接入方为了「配合我们」改坏自己）

1. **不改宿主的扇数**。宿主并发 5 个关键词，就深读 5 批；**不要为了「更好读」压成 1 个**——那是丢方向。
2. **不改宿主的输出格式**。它原来要结论、清单还是报告，都照旧；深读只是把素材质量抬高。
3. **不改宿主的判定**。要不要继续、要不要收敛，仍归宿主（或人）。

#### 接入两步

```bash
# ① 每个扇：同一 query 多打一次 DeepSeek，并把元宝结果落盘，然后合并
python scripts/search.py "<query>" --count 50 --preset brief --show 6
python scripts/save_yuanbao.py                     # 存最近一次内置 WebSearch
python scripts/merge.py <元宝存档> <DS 存档> --keep-source --out <合并>.json

# ② 一条命令读透（清单 / 追原文都在同一条命令里）
python scripts/read.py <合并>.json --limit 10
python scripts/read.py <合并>.json --expand 3,7 --expand-max 1200
```

外部来源（Tavily / Exa / Serper / 自建抓取）先过 `ingest.py` 规范成标准存档，下游零改动：

```bash
python scripts/ingest.py raw.json --list                              # 先看字段
python scripts/ingest.py raw.json --out 存档.json --query "..." --engine tavily
python scripts/ingest.py raw.json --map snippet=highlights,url=link --out 存档.json
```

#### 三行回执（子智能体读完只回这些，宿主照此消费）

```
[深读] query="..." | 批次 58 条（元宝 10 + DS 48）
[结论] ① … ② … ③ …（每条带 [#N]）
[待核] #21 #34 #47 | 存档: <绝对路径>
```

宿主不需要知道存档结构、编号规则、expand 用法；要追证据时把编号回传即可。

#### 并发规模（N = 宿主的扇数）

| N | 打法 | 主轨上下文 |
|---|---|---|
| ≤ 2 | 主轨直读（元宝原文进上下文，判断最准） | ~30–80k 字符 |
| 3–5 | 主轨直读 1 个「锚点扇」，其余委托子智能体，只收 3 行回执 | ~40–60k |
| > 5 | **分批 + 收敛**：先快搜定方向，再对 2–3 个扇深读 | ~30k |

N>5 时不要「全扇深读」（5 × 元宝原文即 125–200k 字符，超出窗口），也**不要压成单扇**（丢方向）——正确动作是分批。

#### 它不做什么

- **不做关键词扇出**：多扇归宿主。一旦做，就要维护「何时扇出 / 扇几个 / 怎么合并」的判断层，与宿主已有的并发策略重复。
- **不做多轮规划与编排**：那是 deep-research 专家团的职责，重写只会更差。
- **不出报告**：它产出「结论 + 编号 + 存档」，报告由宿主或人来写。
- **不抓网页正文**：要正文用 `search.py --need_content` 或自己的抓取器。
- **不接「只给报告、不给 results」的流程**：例如专家团的最终产物是报告文本，没有结果集可插——这不是能力问题，是没有缝。

## 4. 已知局限

1. **引擎很新**：2026-10-17 才对外开放，中文长尾语料覆盖弱于元宝（元宝底层是搜狗索引 + 腾讯生态）。
2. **重复切片**：同一页面常被切成多条返回（实测一个 PDF 的摘要前后两段各算一条），已由 URL 去重兜住，但会损失分节信息。
3. **`authority` 不等于事实正确**：它是来源权威度，低权威不等于错，高权威也不等于对；关键结论仍要回到原文。
4. **计费**：$0.75 / 千次成功请求，失败与空结果不计费。个人检索强度下年成本约个位数人民币。
5. **限流**：Search 100 QPS、Fetch 25 QPS，个人使用不会触及。
6. **`scripts/merge.py` 是实验品，不要当稳定功能用**：它把多个关键词的结果合成一个池子，让**一个**子智能体通读。代码能跑、下游（`dump.py` / `--replay`）能直接读它的产物，但**"读 148 条"到底比"分开读三次"多出什么，从没测过**；而且跨批 URL 去重几乎无效（实测 150 条只去掉 2 条，重复发生在主张层面而不是 URL 层面）。未并入主流程。

## 5. 升级为 MCP（可选，**当前未实现**）

本技能默认以**脚本**形态工作，不需要 MCP——`python scripts/search.py ...` 就是完整功能。只有在想要"常驻工具"形态（工具列表里直接出现、不必每次写命令）时才需要它，而这条路目前是空的。

两步：

1. **先自己写 `scripts/mcp_server.py`**：用官方 `mcp` SDK 包一层，直接复用 `search.py` 里的函数，不要重写 HTTP 逻辑。参数原样暴露；**只返回紧凑文本**——本客户端不解析 `outputSchema` / `structuredContent`，结构化数据不会变成字段，还会因为二次 stringify 把同一份内容塞进上下文两次。
2. 再把下面片段加进 `~/.workbuddy/mcp.json` 的 `mcpServers`，保存后到连接器页面点「信任」才生效（未信任的 server 不会进入 CLI 的 `--mcp-config`，进程不会被拉起）。

```json
"search-deep-read": {
  "command": "python",
  "args": ["<技能目录>/scripts/mcp_server.py"]
}
```

> `<技能目录>` 填安装后的实际路径，不要照抄别人的机器路径。

## 6. 安装与后续维护

> **一条铁律：改技能回项目改，改好再部署到用户级；永远不要在用户级目录里直接改。**
>
> 用户级那份是**产物**，不是工作副本。在它上面改出来的东西没有 git 记录、下次部署必然被覆盖，
> 而且改完就没人知道"线上跑的"和"仓库里的"到底差在哪了。

### 6.1 三个位置，一个真源

| 位置 | 角色 | 谁写 |
|---|---|---|
| 本仓库 `staging/search-deep-read/` | **唯一真源**，所有改动都在这里做 | 改一次配一个 commit |
| `~/.workbuddy/skills/search-deep-read/` | 部署产物，**只读**（手改 = 改产物，见 6.3） | 只由部署命令写入 |
| `<某项目>/.workbuddy/skills/search-deep-read/` | 分叉副本，仅当该项目必须偏离时创建 | 建了就登记偏离点（见 6.4） |

技能正文是**打包进上下文**的：部署只是把新文件放到位，已打开的会话不会自动刷新，**改完要用新会话验证**。

### 6.2 更新流程（每次改完必走）

```bash
# 1 结构校验（放在 skill-creator/scripts/ 下）
python package_skill.py <技能目录> <临时产物目录>

# 2 漂移检查：有输出说明用户级被手改过，先把它回迁进真源，再继续
diff -r <技能目录> ~/.workbuddy/skills/search-deep-read

# 3 单向部署
cp -r <技能目录> ~/.workbuddy/skills/

# 4 装机自检（零 API 成本）：确认换路径后脚本仍能互相找到
python ~/.workbuddy/skills/search-deep-read/scripts/dump.py <任一存档> --stats
```

四步各自防一个坑：跳过 1 会把语法错误带上线；跳过 2 会覆盖掉别人在用户级的手改；跳过 3 等于白改；跳过 4 会在**第一次真实检索时**才发现路径假设已经破了。

### 6.3 改在项目，部署到用户级（不要反过来）

固定动作序列，顺序不能换：

1. **回本仓库** `staging/search-deep-read/` 改
2. 本地先验证（`python scripts/...`，零 API 成本）
3. `git commit`（这次改动有据可查）
4. 走 6.2 的四步部署

**反向做法一律禁止**：直接在 `~/.workbuddy/skills/` 里编辑。三个后果——没有 git 记录、下次部署被覆盖、线上与仓库的差异无从追溯。

已经手改了怎么办：`diff -r` 把它 diff 出来，**回迁进仓库**，再重新部署。不要让仓库去"猜"线上那份是什么。

### 6.4 版本号与分叉

`SKILL.md` 的 frontmatter 带 `version`，格式 `主.次.修`：

- **主**：改契约——存档 schema、编号口径、命令行签名
- **次**：加能力——新参数、新流程、新脚本
- **修**：改文案、修 bug、调默认值

对账方式：`grep '^version' <任一副本>/SKILL.md`，一眼看出用户级落后几版。

项目级分叉只在"这个项目必须用不同版本"时创建。建了就在该副本的 `SKILL.md` 顶部写三行：**偏离什么 / 为什么 / 什么时候合并回真源**。没有这三行就别建——否则三个月后没人记得它为什么存在。

### 6.5 什么该进 git，什么不该

进：`SKILL.md` / `README.md` / `references/` / `scripts/`。
不进：`scripts/__pycache__/`、检索存档（运行时数据，默认落在**使用时的项目**下的 `.workbuddy/search-results/`，不随技能走）。

## 7. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-03 | 初版：`search.py` 全参数实现、URL 去重、authority 过滤、compact/md/json 三种输出；SKILL.md + README |
| 2026-11-03 | 增加产物落盘：原始 JSON 全量存档（完整 summary）+ 日期分目录 + `index.jsonl`；新增 `--replay` 离线重放、`--prune` 清理、`--no-save`/`--out`/`--archive-dir` |
| 2026-11-03 | 三档 preset（brief/default/deep）+ 截断显式标记 `…[截断 a→b]`；`--count` 默认 50、`--show` 独立控制 |
| 2026-11-03 | 新增 lean/full 调研模式与子智能体指令模板；修正两处过时的“默认 180”描述；触发条件标记为待定 |
| 2026-11-04 | **取消 lean/full 双档**，合并为单节「子智能体调度」（两档被用户决定压平到只剩“何时派”一个差异）；触发条件改为可判定谓词 + 「覆盖默认须声明」；指令规范下沉到 `references/subagent-prompt.md`（任务定位从“审计者”改回“回答调研问题”）；SKILL.md 199 → 181 行 |
| 2026-11-04 | 新增 `scripts/dump.py`（读存档：字段白名单 / `--stats` 分布统计），字段定义收敛到一处；指令模板去掉长 `python -c` 命令与实测常数（移入「参考值」段）；「两层读取」与新增的读存档说明合并为「读存档：列表/展开/统计」；SKILL.md 181 → 187 行 |
| 2026-11-04 | **改名 `deepseek-search` → `search-deep-read`**：流程本身与引擎无关，名字不该绑死 DeepSeek（`dump.py` 措辞同步改为“任意含 results 数组的 JSON”）。新增三条机制：① 预算丢弃显式提示 `⚠ 未展示 #11–#50`（此前 `--show 10` 是静默丢弃 40 条）；② 矛盾项改成三列表格（主张/引句/编号），便于跨条目比对；③ 规则「没读到不等于没问题」，未覆盖条目必须显式标注 |
| 2026-11-04 | **取消默认的 authority 预过滤**：引擎顺序即 rerank 降序，二次过滤无收益且会漏掉低权威域上的好内容（实测漏掉模式白皮书/目录/实战文各一）。改为主轨读前 N 条、子轨读全 50，都不筛；`--min-authority` 降为可选开关 |
| 2026-11-04 | **首次实跑派子智能体，修掉 3 个编号/易用性缺陷**（由子智能体的工具反馈暴露）：① 存档新增 `filters` 字段，`search.py --replay` 与 `dump.py` 都据此复现同一套过滤，此前 replay 会多出被过滤条目、编号从 #22 起错位；② `dump.py` 补上与 `search.py` 完全相同的 URL 去重，去掉最后一段偏差点；③ 新增 `--expand-only`（不再重打列表，省 2-3k 字符/次）、`--replay` 可省略占位查询词、dump 分数改成 `auth=3 rerank=1.83` 带标签、空标题回退 URL 末段。实测 33 条编号全部对齐 |
| 2026-11-05 | v1.0.2–1.0.7：脱敏（示例查询改中性、脱掉英文课题 slug）、补 §0 快速开始与 LICENSE、建立公开仓与 `tools/export_public.py` 发布链、description 覆盖「检索」这一层（此前只描述深读，导致「只想用 DeepSeek 搜一下」不触发） |
| 2026-11-06 | 新增 `scripts/save_yuanbao.py`：把内置 WebSearch（元宝）的结果转成同构存档（`engine: yuanbao`、按 `call_id` 去重、`--list`/`--pick` 可回溯历史查询）。**零上下文成本**——读会话记录而非重新输出文本。`dump.py` / `search.py` 遇到缺失的 `authority`/`rerank_score` 改为显示 `—`，不再伪造成 -1/0。版本 1.0.7 → 1.1.0 |
| 2026-11-06 | README 新增 §2.4 双引擎重合度实测（3 对同题同日存档：URL 重合 0–1/10、host 1–3/10、标题相似 1–8/10），说明「页面不重叠但内容可高度重叠」，并记下 `site_name` 不能跨引擎比的方法论坑。版本 1.1.0 → 1.1.1 |
| 2026-11-06 | 新增 `scripts/read.py`（一条命令：批次头 + 清单 + expand，编号与 dump/search 严格一致）与 `scripts/ingest.py`（Tavily/Exa/Serper/自建结果 → 标准存档，缺 url 跳过不编造）；README 新增 §3.6「作为模块接入」（三条硬约束、三行回执、并发规模的三种打法、**它不做什么**）。版本 1.1.1 → 1.2.0 |

## 8. 与 LangChain deepagents 的关系（2026-11-04 评估）

`deepagents` 是 LangChain 的独立 Python 包（MIT，24k stars），核心能力是「虚拟文件系统 + 子智能体委派 + 上下文卸载」。**本技能做的事，是它的一个子集**：

| deepagents 的能力 | 我们的对应实现 | 是否需要改动 |
|---|---|---|
| 大工具结果卸载到磁盘、只返回路径 | `search.py` 落盘 + 存档 | 已等价，无需改 |
| 子智能体读文件、只回摘要 | `references/subagent-prompt.md` 的流程 | 已等价 |
| 文件工具带 `offset`/`limit` 分页 | `dump.py --limit/--ids` | 已等价 |
| 技能渐进披露（SKILL.md → references/） | 同结构 | 已等价 |
| `SubAgent` 定义只有 5 个字段：`name`/`description`/`system_prompt`/`tools`/`model` | WorkBuddy 的派发接口本来就是 `(name, description, prompt, subagent_type, model)` | **已天然对齐，无需改** |
| `write_todos` 计划跟踪 | 未做 | 不做——我们的子任务只有 2 轮，加计划层是负担 |
| LangGraph runtime / checkpointer | 无（WorkBuddy 技能体系里没有） | 无法复用，也不需要 |

结论：**代码零复用，接口已对齐。** 参照 deepagents 的实际收益是**确认我们没走偏**（它的核心卖点就是「卸载到磁盘 → 子智能体读 → 只回摘要」，和我们同一套），加上术语统一（retrieve / offload / delegate），便于以后查资料和迁移。不产生代码改动。

## 9. 许可证

MIT，见 [LICENSE](LICENSE)。
