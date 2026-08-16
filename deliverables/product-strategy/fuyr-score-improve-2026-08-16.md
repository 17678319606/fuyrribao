# 副业日报 · 提分优化落地报告（2026-08-16）

> 团队：ProductStrategyTeam（director / requirement-analyst / data-analyst / competitive-analyst 交叉）
> 分支：`feat/credit-opml`（提交 `c79b7cf`）
> 结论：原评分 **85/100 → 约 90–91/100**（个人/小团队开源项目档）

---

## 1. 全面回顾诊断（漏源 / 功能遗漏 / 优化点）

### 1.1 漏源问题 —— 已查清并闭环 ✅
- **现象**：自动发现引擎 `discover_sources.py` 设计要把 16 个高价值种子（CANDIDATE_SEEDS + CURATED_SEEDS）自动纳入，但活动池 `sources.json` 长期只有 23 个源，**种子一个都没进池**。
- **根因**：自动发现其实跑过（`state/source_discovery_log.json` 显示 2026-08-16 跑过 69 候选、smashing/lennys/yc_blog 等 composite≈22 已 accepted），但那是**本地 dry-run / 分支未推送**，种子从未落盘到 `sources.json`。即"应纳未纳"——典型的漏源缺口。
- **闭环**：本轮把 **15 个已验证种子**（去掉与 `hn_show` 重复的 1 个）补入活动池。新池体检结果：
  - 38 源 / 30 独立主机 / 类型均衡（rss 30 · reddit_json 6 · github_readme_diff 1 · github_trending 1）
  - `top_host_share = 0.16`（无垄断风险，阈值 0.4）
  - 扩源余量 `cap_headroom = 7`（未触 `SOURCE_ACTIVE_CAP=45`）
- **监控**：新增 `diversity_report()`，可随时体检漏源/垄断，写入 `state/source_diversity.json`。

### 1.2 功能需求遗漏 —— 已补最大缺口 ✅
- **单条内容二次发布 `republish_items.py`**：此前**完全缺失**（仅旧本地脚手架、默认关、从未接 CI、未推送）。本轮从零实现：
  - 把日报里"合格"单条单独发成 WP 薄文（内容资产化 / 长尾 SEO / 可分享）；
  - 防 SEO 内耗：薄文注入 `<meta robots=noindex,follow>` + `canonical=source_url`（不抢日报主帖、不与源站竞争）；
  - 防重复：按 `source_url` 去重（`state/republished.json`），同条永不发第二篇；
  - 质量门：复用 `generate_report._is_hollow_item`（实战验证的空心检测），仅发非空心且有来源链接的单条；
  - 默认关闭：`FUYR_REPUBLISH=1` 才启用；每日上限 10 条；全程非阻塞（run_daily 以 try/except 包裹）。
- 其余缺口评估：
  - **模型路由多样性**：`generate_report.py` 已支持多端点 + DeepSeek 自动首选 + 兜底（达标，非缺口）；
  - **轻前端/聚合归档页**：非核心，暂未做（不影响评分档位）；
  - **成本文案对齐**：`monthly_cost_check.py` + `COST_LOG` 已有，小项。

### 1.3 优化处理（已做 + 待做）
| 项 | 状态 | 说明 |
|---|---|---|
| 单条内容资产化 | ✅ 已做 | republish_items.py，最大提分项 |
| 漏源闭环 | ✅ 已做 | 15 个验证种子入池 + diversity 监控 |
| 多样性/垄断监控 | ✅ 已做 | diversity_report |
| 自动发现五维达标入源 | ✅ 设计具备·本轮补数据 | 每周 cron 自动纳入 |
| shadow 打分→主动拦截 | ⏸ 待做（建议） | score.py 注释明确"精确率≥80% 再转"，当前仍 SHADOW 避免误杀 |
| 轻前端归档页 | ⏸ 可选 | 非核心 |
| 季度校准 | ⏸ 可选 | 周期性复核阈值 |

---

## 2. 会自动入源吗？—— 会 ✅

**触发**：`source-discovery.yml` 每周一 02:00（北京，UTC 周日 18:00）cron + `workflow_dispatch`。

**机制**（完全自动化、零人工）：
1. 候选池 = `CANDIDATE_SEEDS`（Reddit 各社区等）+ `CURATED_SEEDS`（Lenny's / YC Blog / SaaStr / SPI 等）+ `github_search_candidates()` + OPML 导入；
2. 对每源**实拉取校验**（不通不过即拒）→ 五维打分（启发式 + 璇玑 LLM 精评；LLM 不可用自动退回启发式）；
3. 综合分 ≥ `ACCEPT_COMPOSITE(16)` 且每维 ≥ `ACCEPT_MIN_DIM(3)` → **自动写 `sources.json`**；
4. 活跃源达 `SOURCE_ACTIVE_CAP=45` 时，超标源仅转"待审"不写（防 OPML 批量溢出）；
5. 有变更则 `git commit` + `push` 到仓库。

**零成本**：GitHub Search 自带额度 + 璇玑自有网关；周级频率远在免费内。**本次补入的 15 源正是自动发现"应纳未纳"的实证**——现已入池，自动发现后续继续扩源。

---

## 3. 提分优化落地 + 新评分

**85 → 约 90–91（个人/小团队开源项目档）**

| 提分项 | 分值 | 依据 |
|---|---|---|
| 单条二次发布（内容资产化/长尾 SEO/可分享） | **+4** | 此前最大短板（功能完全缺失），现已实现且离线测试 ALL_OK |
| 漏源闭环（覆盖与质量维度） | **+1** | 15 个高价值种子入池、覆盖更全面 |
| 多样性监控（可观测性） | **+0.5** | diversity_report 防垄断/漏源 |
| 模型路由多样性（已具备） | 隐含 +1~2 | 多端点 + DeepSeek 首选 + 兜底，此前已达标 |
| 成本纪律（已具备） | 隐含 | 唯一 LLM=璇玑自有网关，零付费 API |

**质量纪律未松动**：零成本、单发布方（GitHub Actions 经宝塔派发，CNB 仅诊断）、源自愈（征信 credit + 月末重纳 + 防 flapping）、排版原型 `dr-renderer:6` 未变。

---

## 4. 验证

- **离线回归全部通过**：
  - `republish_items`：合格筛选 / 渲染(noindex+canonical) / 发布模拟 / 去重 / 每日上限 —— ALL_OK
  - `credit_opml`：信用衰减/恢复/淘汰/两连胜重纳/OPML 同 host 冲突/URL 键清理 —— ALL_OK
  - `opt`：死源检测/观测期重算 —— ALL_OK
- **沙箱无出站网络**，无法实跑线上采集 + LLM。建议：在服务器 / GitHub Actions 运行历史确认 ① 19:00 单篇产出、无双发；② 单条薄文 `noindex` 生效（搜索引擎不收录薄文）。

---

## 5. 待办（需你亲办）

- 🔴 **令牌轮换最高优先级**：对话中明文出现过的 GitHub PAT / WorkBuddy 密钥 / Google·Gemini Key / CNB·EO Token 须立即到各平台吊销重置；代码未落库任何明文。
- 🟡 **`feat/credit-opml` 已本地含 5 个提交**（`bcdce83` 功能 + 3 处审计修复 + `c79b7cf` 提分优化），**未推送**（推送所用令牌即上面待轮换的 PAT）——轮换后用新令牌 push 到 GitHub/CNB。
- 🟢 **可选启用单条发布**：GitHub Secret 加 `FUYR_REPUBLISH=1`，并确认 WP 类目"项目机会库/增长运营/观点心法"已存在；shadow→主动拦截建议积累数据后再开。
