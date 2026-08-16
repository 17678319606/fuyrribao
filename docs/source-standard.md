# 内容源标准（Content Source Standard）

> 用途：作为 fuyrribao「自主扩源」的唯一裁决依据。AI（发现脚本 / Skill）按此标准
> 对候选源打分，**达标才自动写入 `scripts/sources.json`**，不达标仅记录供人工审阅。
> 目标：让日报在"零人工运维"下长期保持新鲜度与内容多样性，且源的稳定性/成本可控。

## 0. 总原则（铁律）

1. **不自建源**：绝不自己搭爬虫/中转/聚合服务（运维成本与稳定性风险高）。只接入
   现成的 RSS/Atom/官方 JSON API/社区公开 feed。发现脚本也只能"接入"，不能"生产"源。
2. **单一发布方 + 单一抓取方治理**：源抓取与发布都走 GitHub Actions 主力流水线；
   CNB 仅做生成诊断（已设 `FUYR_DISABLE_PUBLISH=1`），避免双 CI 打架。
3. **阈值门控 + 留痕**：任何源被自动引入，必须经五维打分且**每项 ≥3、综合 ≥18/25**，
   并写入 `state/source_discovery_log.json`（谁、何时、为什么、几分）。绝不静默写入。
4. **免费额度内**：发现脚本只用 GitHub Search API（自带额度）+ 璇玑 LLM 打分
   （用户自有网关，成本极低）。LLM 不可用时退回纯启发式打分，不阻断。

## 1. 五维打分维度（每项 1–5）

| 维度 | 含义 | 5 分 | 1 分 |
|---|---|---|---|
| **相关度 relevance** | 内容与"副业/AI/独立开发/增长/创业"主题的契合 | 明确属于上述主题、几乎每篇可用 | 泛科技/无关 |
| **稳定性 stability** | 源本身的可用性与抗变能力 | 官方/老牌社区、标准协议、无需鉴权 | 个人临时页、需 JS 渲染、常 404 |
| **格式 format** | 机器可解析度（决定抓取成本与可靠性） | RSS/Atom（标准、易解析） | 需 HTML 爬取/JS 渲染 |
| **稀缺性 scarcity** | 内容是否被现有源覆盖（避免重复） | 现有源未覆盖的独特点/社区/语种 | 与现有源高度重叠（同质化） |
| **权威度 authority** | 发布方可信度与编辑质量 | 官方/知名社区/资深作者 | 低质聚合/营销号 |

## 2. 准入阈值

- **自动化引入（AI 自写）**：五维**每项 ≥ 3** 且 **综合分 ≥ 18/25**。
- **仅记录待审（不自动引入）**：综合分 14–17，或任一维度 = 2；写入日志供人工决策。
- **直接拒绝**：综合 < 14，或任一维度 = 1（如格式=1 需爬 JS、或稳定性=1 常 404）。

## 3. 评分方法（两层）

- **L0 启发式（永远可用，零成本）**：
  - 格式分：由 `type` 直接定（rss=5 / reddit_json=4 / github_trending=4 / github_readme_diff=4），
    并实拉取校验"确为可解析 feed"才给分，否则格式=1。
  - 权威分：`AUTHORITY_TIERS` 域名表命中得 4–5，未命中默认 3。
  - 相关度：主题词表（`TOPIC_LEXICON`）对"域名+名称+样例标题"命中数映射（≥3→5，≥2→4，=1→3，0→1）。
  - 稀缺性：域名/名称已存在于 `sources.json` → 1（重复）；否则 4（默认新颖）。
  - 稳定性：由 `type` + 是否需要鉴权定（标准协议=5，reddit=4，需 JS=1）。
- **L1 LLM 精评（璇玑，可选）**：对通过 L0 的候选，把"URL + 样例标题"交给 LLM，
  返回五维分数与一句话理由，覆盖 L0 的 relevance/authority 估计。
  LLM 失败则退回 L0，不阻断引入。

## 4. 自主发现流程（发现脚本 `scripts/discover_sources.py`）

1. 载入 `sources.json`（现有源，用于去重/稀缺判断）。
2. 生成候选：
   - **种子清单 `CANDIDATE_SEEDS`**：人工精选的高相关社区/RSS（Reddit 各主题社区、
     HN Show、Smashing 等），复用现有解析器类型，零新增代码。
   - **GitHub Search（best-effort）**：用 `topic:side-hustle` / `indie-hacker` / `ai-tools`
     等检索高 star 仓库，取其 `homepage`；仅当 homepage 能被校验为 feed 才进入候选。
3. 对每个未收录候选：`validate()` 实拉取校验 → L0 打分 → L1 精评（可选）→ 合成综合分。
4. 达标 → 追加到 `sources.json`（带 `added_by:"auto-discover"`、`added_date`、`score` 元数据）；
   不达标 → 仅写入日志。
5. 脚本只改文件、不碰 git；提交由流水线步骤完成（实现"AI 写源 + 自动持久化"）。

## 5. 治理与回滚

- 每次自动引入都在日志留痕；`sources.json` 的 diff 即审计线索。
- 若某自动引入源后续失效/降质，`fetch_signals` 的"源抓取失败告警"会提示，
  人工或后续"源健康巡检"可将其移出 `sources.json`（暂定手动，成本极低）。
- 不追求"越多越好"：稀缺性维度天然抑制同质源堆砌；源总量建议维持 25–40 之间。

## 6. 与"打分地基（内容筛选）"的关系

- 本标准是**源的**准入标准（回答"哪些源值得抓"）。
- 内容筛选的"打分地基"（`themes/sidehustle.json` + `scripts/score.py`）是**条目的**
  质量评分（回答"抓来的内容里哪些是好内容"）。
- 两者独立：换主题时，源标准微调词表即可；内容打分地基的"通用层"不变、只换"主题层" yaml。

## 7. 内容源容量管理系统（运行时打分 → 动态容量上限 → 生命周期）

> 与 §1–§4「准入标准」互补但不同层：准入标准决定"哪些源值得加进来"；
> 本系统决定"加进来之后，每个源每轮最多抓多少、何时毕业/淘汰"。
> 实现见 `scripts/source_manager.py`（零 LLM 成本，纯运行时指标 + 启发式相关度）。

### 7.1 为什么还要"锁容量上限"（重新评估结论）

锁容量**值得且必须**，三条硬理由：
1. **防单源垄断**：中年指南 / aggregator 类高产源会淹没其他源，压低多样性；
2. **保质量下限**：低质源不该占满候选预算，额度应让给高质源；
3. **锁资源上限**：fetch HTTP 次数、AI 上下文长度、LLM 成本都随候选量线性增长。

但"锁"不等于"一刀切"——本系统在锁总量前提下做精细化：按综合分把预算加权分给每源。

### 7.2 四维综合分（0–100，权重 相关0.30/稳定0.25/产量0.20/质量0.25）

- **相关 rel**：`relevance` curated(1–5) 或 `heuristic_relevance()` 启发式（不调 LLM）；
- **稳定 stab** = fetch_ok / fetch_total（来自 `source_status.json` 抓取成败）；
- **产量 yield** = min(1, 日均有效候选 / `SOURCE_YIELD_REF=20`)；
- **质量 qual** = min(1, 日均成卡 / `SOURCE_QUALITY_REF=1.5`)（来自 generate 后成卡统计）。

`score = 100 × (0.30·rel + 0.25·stab + 0.20·yield + 0.25·qual)`。

### 7.3 动态容量分配（fetch_signals 实际生效）

- 每活跃源 cap = `clamp(round(BUDGET·w_i/Σw), CAP_MIN=5, CAP_MAX=120)`，`BUDGET=600`；
- 高分源更高 cap（提质量），但 `CAP_MAX=120` 封顶 + 全局 `MAX_CANDIDATES=1500` 兜底（防垄断）；
- 观测期(trial)源固定 `CAP_TRIAL=15`；冷启动无指标时均匀回退。
- **与 §1 准入标准、`score.py` 条目打分、`BALANCE_*` 多样性均衡的关系**：
  fetch 阶段先按"本系统 cap"截断每源（按运行指标打分），再走 `BALANCE_*` 多样性采样
  （防单源占比过高）——三层各管一段，互不冲突。

### 7.4 源生命周期（默认仅建议，落地需 `FUYR_SOURCE_AUTOMATION=1`）

- **观测期 trial**：新源小 cap 试运行，积累 `SOURCE_INCUBATION_RUNS=8` 轮指标；
- **晋升替换**：达标（`eval_score≥60` 且 `fetch_ok_rate≥0.6`）→ 转 active，并替换当前
  最低分 active 源（被替换源进 `retired` 备份，不删，可回滚）；
- **死源淘汰**：`SOURCE_DEAD_DAYS=120` 天无有效贡献，或 `SOURCE_DEAD_FAILS=14` 次连续
  抓取失败 → `retired`（保留记录可回滚）。
- 所有判定写 `state/source_actions.json` 审计；只有显式 `FUYR_SOURCE_AUTOMATION=1`
  才改 `sources.json`，绝不静默改动。

### 7.5 资源消耗

全部为本地计算（字典/算术），**零 LLM 调用、零额外网络请求**；指标落盘
`state/source_metrics.json`，建议由 `scripts/monthly_cost_check.py` 每月核对 AI 成本。
