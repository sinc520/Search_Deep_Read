---
name: search-deep-read
version: 1.0.7
description: 对单次搜索结果做深度解读，并内置 DeepSeek 搜索 API 的调用封装（单次最多 50 条、按请求计费、多取不加价）。两层用法——① 检索：需要一手源、学术论文、技术文档、英文资料，或需要超过 10 条结果时，用它替代内置 WebSearch；② 深读：把这一次返回的整批结果存档，主轨先读前 10 条形成结论，再派子智能体读全量、只回传增量（矛盾/遗漏/可疑/待核），需要精确引文时定向展开原文。当需要在一批结果里地毯式找全线索（宁可慢也不漏）、结果要写进产物、前几条互相矛盾、或怀疑来源高度同质（看着十来条其实是同一篇的镜像）时使用。单跳事实查询、容错高的探索、时效新闻不要用；中文资讯、政策与企业动态改用内置 WebSearch（腾讯元宝）。
agent_created: true
---

# Search 深读（search-deep-read）

## 何时使用

本技能有两层，门槛差很多，别混：

**层一 · 用它检索**（`search.py`）—— 门槛低，经常用

| 查询类型 | 用哪个 | 原因 |
|---|---|---|
| 学术论文、技术文档、API 手册、英文资料 | **本技能** | 实测同一查询下，DeepSeek 10 条中 8 条为一手源，内置 WebSearch 为 0 条 |
| 需要超过 10 条结果 | **本技能** | 内置 WebSearch 服务端硬顶 10 条；本技能可到 50 |
| 需要看来源权威度分级 | **本技能** | 只有本接口返回 authority（但**别拿它当过滤器**，见「读结果」） |
| 中文资讯、政策、企业动态、股价财报 | 内置 WebSearch | 元宝接入了 157 个权威信源与腾讯生态（微信/新闻/财报），中文时效更好 |
| 中文技术长尾（如某国产软件配置） | 两者都试 | DeepSeek 中文长尾覆盖弱于元宝 |

**层二 · 用它深读**（派子智能体）—— 门槛高，**10 次里约 1–2 次**

只有当结果要写进产物、前几条互相矛盾、或怀疑来源高度同质时才派。其余情况主轨读前 10 条就够了，不要派——它会让单轮从 10 秒变成 1.5–3 分钟。判据见「子智能体调度」。

## 快速用法

```bash
# 默认：10 条、紧凑输出（标题/站点/日期/URL/摘要）
python scripts/search.py "某研究主题 关键词A 关键词B"

# 学术检索：限 arXiv、取 20 条
python scripts/search.py "RAG optimal retrieval volume" --site arxiv.org --count 20

# 中文资料 + 语言限定
python scripts/search.py "某研究主题 N个关键词 最新进展" --lang zh --count 10

# 需要原始结构做二次处理
python scripts/search.py "query" --format json
```

密钥自动从 `~/.workbuddy/models.json` 中 URL 含 `api.deepseek.com` 的条目读取，也可用 `--api-key` 或环境变量 `DEEPSEEK_API_KEY` 覆盖。

## 参数速查

| 参数 | 说明 |
|---|---|
| `query` | 检索词，1-128 字符，可直接内联 `site:` / `after:` / `lang:` 算子 |
| `--count` | 向 API 请求的条数 1-50，**默认 50**（按次计费，多取不额外花钱） |
| `--preset` | 展示档位 `brief` / `default` / `deep`，见下方三档表 |
| `--show` | 本地展示条数（覆盖 preset，0=全部）；只影响进上下文的量 |
| `--expand` | 输出指定序号的**完整 summary**，如 `2,5` 或 `1-20`；可展开未展示的条目。**可重复给**（`--expand 1 --expand 8`）|
| `--expand-only` | 只输出展开内容，不重打列表——省掉重复的 2-3k 字符 |
| `--expand-max` | expand 时的截断字数，默认 0（不限制） |
| `--field` | 列表用 `summary` 或 `snippet`（覆盖 preset） |
| `--ori-question` | 用户原始问题，提升意图理解（查询被改写时传原文） |
| `--localization` | 地域化偏好（如 `zh-CN`），影响结果的地域相关性 |
| `--site` / `--exclude-site` | 站点限定/排除，可重复；多站点自动拼成 `(site:a OR site:b)` |
| `--lang` `--after` `--before` | 语言与时间过滤，拼成官方算子 |
| `--min-authority` | **默认不筛**。引擎已按 rerank 降序返回，直接读前 N 条即可。仅在明确要排除低权威源时才设 3；`--replay` 沿用存档记录的值 |
| `--sort` | `rerank` 按相关度 / `authority` 按权威度 / `none` 保持原序 |
| `--need-content` | 返回网页全文，**极吃上下文，仅在摘要不足时使用** |
| `--no-dedup` | 关闭按 URL 去重（默认开启） |
| `--format` | `compact`（默认）/ `md` / `json` |
| `--max-summary` | 每条摘要截断字数，默认随 preset（brief 300 / default 1000 / deep 不限）；只影响展示 |
| `--no-save` | 不落盘（默认每次都存原始 JSON） |
| `--out` | 指定存档路径 |
| `--archive-scope` | `project`（默认，存到 `<项目>/.workbuddy/search-results`）/ `global` |
| `--archive-dir` | 直接指定存档根目录，优先级最高 |
| `--replay FILE` | 离线重放存档，**不调用 API**；此时可不写检索词，编号沿用存档的过滤口径 |
| `--prune DAYS` | 删除早于 N 天的存档目录后退出 |

## 产物落盘

每次搜索默认把**原始响应全量**写入本地，避免重复付费与无法溯源。

**默认落在项目目录**，与项目数据同处、便于随项目备份：

```
<项目>/.workbuddy/search-results/          # 默认（archive-scope=project）
├── index.jsonl                            # 一行一次搜索：时间/查询/条数/存档路径
└── 2026-11-03/
    ├── your-topic-keywords-103838-4fc4.json
    └── q-7c1e2b9d-104203-55e1.json        # 全中文查询用哈希命名
```

跨项目复用的检索（如某 API 文档）用 `--archive-scope global` 存到 `~/.workbuddy/search-results`，否则在 B 项目看不到 A 项目的存档。

路径解析优先级：`--archive-dir` > 环境变量 `DEEPSEEK_SEARCH_ARCHIVE` > `--archive-scope` 指定的项目/全局目录（项目目录取自 `CODEBUDDY_PROJECT_DIR`，回落当前工作目录）。

存档内容：`query_raw` / `query_final` / 完整 `request` / `elapsed_ms` / 每条结果的 **未截断 `summary`、`snippet`、`authority`、`rerank_score`、`date`、原始 `url`**。实测 6 条结果约 22 KB，`summary` 最长可达 4500+ 字符——**这些内容默认不进上下文，只在需要时按需读取**。

使用方式：

- 先在 `index.jsonl` 里定位（`grep 关键词 index.jsonl`），不要遍历目录
- 要复用旧结果：`--replay <file> --sort authority`（按权威度重排），零 API 成本
- **不要整文件读进上下文**，用 `python -c` 或 `grep` 只取需要的字段
- 网页正文（`content`）默认不存；需要时用 `--need-content` 单独取，并自行落盘到别处
- 项目目录若纳入 git，记得把 `.workbuddy/search-results/` 加进 `.gitignore`

## 读结果

每条结果前缀 `[authority|rerank_score]`：

- **authority 0-4**：来源权威度。4 = Nature/NIH/ScienceDirect/ACM 等，3 = 体面站点，≤2 = 一般网页，-1 = 未知。
  **它衡量的是域名眼熟程度，不是内容质量**——实测被它判为 2 的条目里有设计模式白皮书、Awesome 系列目录、以及元数据抓取失败的好页面。所以默认只当参考字段读，不用它做过滤。
- **rerank_score 0-3**：相关度+权威+时效的合成分。官方口径 **≥1.5 视为高质量**。

返回条数会因为去重（同 URL 多段）略少于 `--count`，这是预期行为，不是失败。

## 三档展示（--preset）

一次搜索固定请求 50 条并存档，**展示档位只决定进上下文的量**：

| preset | 字段 | 每条上限 | 条数 | 实测总字符 | 适用 |
|---|---|---|---|---|---|
| `brief` | snippet | 300 | 10 | **5.1k** | 快速扫一眼有没有相关结果 |
| `default` | summary | 1000 | 10 | **11.1k** | 常规检索，信息量对齐元宝（元宝每条中位 1043 字） |
| `deep` | summary | 不截断 | 5 | **21.7k** | 只看了 5 条但每条都要完整（英文约 5k token，中文可达 21k） |

数字来自同一份存档（50 条英文结果）的实测，非估算。

选择依据：元宝每条 snippet 中位 1043 字、DeepSeek snippet 中位 330 字、summary 中位 2511 字。`default` 的 1000 字正好对齐元宝的实际体验，`brief` 则是 DeepSeek 原生 snippet 的原始密度。

## 读存档：列表 / 展开 / 统计

默认行为是"**存档全量、上下文少量**"。三种读法：

| 想知道什么 | 命令 |
|---|---|
| 列表（编号/权威/站点/日期/标题/snippet） | `python scripts/dump.py <存档>` |
| 只看其中几条 / 换字段 | `dump.py <存档> --ids 3,7` 或 `--fields title,url` |
| 主轨那 10 条是否同质化 | `python scripts/dump.py <存档> --stats --limit 10` |
| 某几条的完整 summary（0 API 成本） | `python scripts/search.py --replay <存档> --expand 3,7` |

```bash
# 搜：API 取 50 条、存档 50 条、上下文只显示 10 条
python scripts/search.py "某研究主题 最新进展"
# 搜完立即展开前 3 条
python scripts/search.py "query" --expand 1-3
```

**结论：列表只够判断相关性，结论必须靠 expand。** 不要指望列表给数字。
实测依据（为什么 180 字不够、1000 字怎么定的）见 `README.md` 第 2 节。
`--stats` 只输出事实，不输出"该派 / 不该派"——阈值是拍出来的，判断权归主轨。

**编号约定（必须严守）**：列表、`dump.py`、`--expand` 三处的编号**一律是「过滤 + 去重后」的序号**，不是存档原始下标。三个脚本读同一个 `filters` 字段复现同一套过滤，所以「看第 12 条」指的是同一条。dump 加 `--all` 可看未过滤的全部，那时编号会变。

## 子智能体调度

派它是一次**上下文减负**：同一件调研任务，子轨读全部 50 条，主轨只读 10 条。
成本实测：进子轨约 16.5k 字符（50 条 snippet），回传约 900 字，压缩约 18:1。

**默认不派。** 三种情况派，但性质分两类：

| 触发 | 性质 | 判断方式 |
|---|---|---|
| 用户说「调研模式 / 要全面 / 要反例」 | 指令，无条件是 | 不判断，直接派 |
| 主轨 10 条同质化：同一站点 ≥3 条，或全部同一立场 | **我的判据** | 可数：`dump.py --stats --limit 10` 看站点/权威分布 |
| 主轨结论与同主题既有存档冲突 | **我的判据** | 可查 |

上表后两条的双重作用：**命中 → 我应当派**；**命中但我仍决定不派 → 必须说明为什么**。
这只是我否定自己的判据时要出声，不是每次搜索都要交代。没命中时不派也不用说。
「我觉得信息不足」不算信号，无法自审，不许拿它当理由。

派了之后：

1. 主轨先读 default 档 10 条，出初步结论
2. 子轨读全部 50 条 snippet（**含主轨那 10 条**）→ 回答同一问题，只交增量
3. 合并：冲突项必自己 expand；子轨标「可疑」的抽验（实测它误判率约 1/2）

子轨为什么不只读剩下 40 条：它需要全局视角才能判断「主轨那 10 条有无系统性偏差」，只读剩余部分会失去这个判断位。重叠不是浪费——主轨读 summary（深），子轨读 snippet（广）。

指令模板、D/E 分离规则、落盘约定见 `references/subagent-prompt.md`（**只在派发前读**，不要提前加载）。

## 关键注意

1. **上下文成本按档位算**：brief 约 5k 字符、default 约 11k、deep 约 22k（均为 10 条实测值）。default 档上限 1000 字已对齐元宝体验，非特殊情况不要把 `--max-summary` 调到 1500 以上；真要读长文用 `--expand` 精确取。
2. **`--need-content` 慎用**：单条全文可达数千字，10 条就能吃掉几万 token。需要正文时优先把 URL 交给 WebFetch 单独取。
3. **同一页面会被切成多条**：实测一个 PDF 摘要的前后两段会作为两条结果返回，因此默认按 URL 去重。关闭去重只在需要看分节内容时才有意义。
4. **失败会重试但 4xx 不重试**：401（密钥错）、402（余额不足）、429（限流）直接报错退出，不要盲目循环调用。接口限流 Search 100 QPS。
5. **计费**：每次成功请求计费（$0.75/千次），失败与空结果不计费。空结果是 `exit 2`，不是异常。
6. **列表永远是截断的**：`--preset` / `--max-summary` 只控制展示，存档里永远是完整版。两处标记要认：
   - `…[截断 2460→1000]` —— 这一条只看到开头，要结论必须 expand
   - `⚠ 未展示 #11–#50 共 40 条` —— **整批只看了前 N 条**，后面 40 条一个字没进上下文。别把前 10 条当成全部
7. **`snippet` 是查询相关的中段切片，不是页面开头**：实测 63 条中，snippet 在 summary 里的起始位置分布在 0–3605 字符，仅 25% 落在开头；内容多为结论句（"We demonstrate…"、"these results highlight…"）。因此列表用 `--field snippet` 比用 `summary` 头部信息密度高约 6 倍。

## 相关文件

- `scripts/search.py` —— 检索与存档，无第三方依赖，Python 3.10+
- `scripts/dump.py` —— **读存档**：提取字段白名单（给子智能体）或输出站点/权威/重复分布。字段定义只在此一处，改字段改它，不用动文档
- `references/subagent-prompt.md` —— 派子智能体时才读的指令规范
- `scripts/merge.py` —— 【实验·未并入流程】跨存档合并，多关键词结果合成一个池子。见 README「实验区」
- `README.md` —— 选型依据、实测数据、MCP 启用方式、局限
