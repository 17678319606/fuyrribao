# 副业日报（fuyrribao）· 详细流程与实现说明 + 85 分提升空间
**日期**：2026-08-16　**基线**：`feat/credit-opml` @ `c705cc7`
> 本文所有结论均**直接读取当前仓库代码**核实，非凭记忆。

---

## 0. 一句话总览
- 日报自动更新 WP：**已落地、稳定** ✅
- 单条内容自动更新 WP：**未落地**（规划过、脚手架曾在旧本地副本、未进同步仓、未接 CI）⚠️
- 公众号自动更新：**已落地、稳定**（走 WP 插件，非 CI）✅
- 排版原型：**未变**（RENDERER_VERSION=6，自 `3e8382b` 后无人改）✅

---

## 1. 日报稳定自动更新（已实现）
**触发链（单发布方，无双发）**
```
宝塔 cron 19:00(北京) → trigger_daily.sh(flock防重 + GitHub Dispatch API)
  → GitHub Actions daily-report.yml (workflow_dispatch，无自有 schedule)
  → run_daily.py（单入口）
      ① fetch_signals.py     ② generate_report.py     ③ publish_wp.py     ④ notify.py
```
CNB `.cnb.yml` 06:00/19:00 仅诊断（`FUYR_DISABLE_PUBLISH=1`），不发布。

**① 采集 `fetch_signals.py`**
- 读 `scripts/sources.json`（当前 42 源，type=rss）。
- 按源均衡采样：总候选 > `BALANCE_TRIGGER=300` 即触发均衡；普通源单源上限 `ceil(BALANCE_TARGET=900 / 组数)`≈22，高配额源 `HIGH_SOURCE_CAP=70` → 防单源垄断（如早年"中年指南"占 44% 已修复）。
- 硬上限 `MAX_CANDIDATES=1500` 安全网；不可达源优雅拒绝（超时/502 → ok=False 但流程不崩）。

**② 生成 `generate_report.py`**
- 免费预筛 `_prefilter_signals()`（**零 LLM**）：去重 + 丢标题+正文 < `PREFILTER_MIN_LEN=80` 且无关主题词的桩 → 降噪省 AI 调用。
- 璇玑网关（`ai.jinbufenzi.com/v1`，自有、免费）做筛选/去重/空心拒收。
- 每模块条目封顶 `MAX_ITEMS_PER_MODULE=8`（三模块合计通常 12–24，保证可扫读）。

**③ 发布 `publish_wp.py`**
- `render()` → 卡片式 HTML（`dr-renderer:6`，**纯 inline style**，对 WP wpautop 免疫，clamp() 响应式）。
- WordPress REST API 直接发布（非草稿）；偶发 429/5xx 自动重试退避。
- **同日增量累积**：同 date slug 覆盖更新，**绝不产生第二篇**；`generate` 标记 `.gen_changed=0` 时跳过冗余更新。

**④ 通知 `notify.py`**
- 失败立即企微告警；末次触发（≥19 时）成功汇总一条摘要；晨间成功不发（防刷屏）。

**幂等/防重三层**：GitHub `concurrency: fuyrribao-daily` + 同日 slug 覆盖 + 宝塔 `flock`。

---

## 2. 单条内容自动更新 WP（未落地 · 仅规划）
**诚实结论**：当前同步仓 `scripts/` 下**不存在 `republish_items.py`**。
- 早期会话曾在旧本地副本 `/tmp/fuyr-push` 建过脚手架，但**默认关、未接 CI、从未推送**到 GitHub/CNB 同步仓。
- 因此"合格单条内容按规划自动二次发布到 WP"——**目前没有在跑**，也不是当前代码能力。

**若要做（已设计、未激活）**：`republish_items.py` = 选中的高质量单条 → canonical 回日报 + 薄页 `noindex` + 去重 + 标签 AI 建 + 只映射已有分类。
- **启用前置**（避免 SEO 风险）：先建枢纽/分类页 + Schema + 内链 + Search Console 基线，再每周 ≤10 篇灰度。
- 落地动作需我重建脚本并接线 CI（默认仍关，由开关控制）。

---

## 3. 公众号自动更新（已实现 · 稳定）
- CI 的 `publish_wechat.py` 步骤在 `WX_PUSH_VIA_WP=1` 时**被 run_daily.py 跳过**（第 87 行）。
- 真实推送由 **WordPress 插件 `fuyr-wechat-pusher`** 在源站于 WP 文章发布后自动触发（利用服务器已在微信白名单的出口 IP）→ **稳定可达，且避免 CI 与插件双推**。
- `publish_wechat.py` 仍保留作本地/故障 fallback（含 `WX_PROXY_URL` 代理解决 GitHub 动态 IP 白名单问题）。

---

## 4. 源自愈闭环（自动化一部分）
- `discover_sources.py`：周级（`source-discovery.yml` 北京周一 02:00）+ `CANDIDATE_SEEDS`/GitHub Search；`validate_source` 实拉校验 + 五维评分，综合≥16 且各维≥3 → 自动写 `sources.json`（`added_by:"auto-discover"`）。免费（GitHub Search 额度 + 璇玑）。
- `source_manager.py`：credit 征信（成功+3/失败-8，0–100）；`credit≤20` 淘汰为 `retired`（备份不删）；`review_retired()` 月末实拉校验，恢复+25、连胜≥2 且 credit≥50 → 重纳为 trial（防 flapping）。
- `source-credit-review.yml`：每月 28 日 18:00 UTC≈北京 29 日 02:00 月检；`dry_run=true` 仅审计不落地。
- 开关 `FUYR_SOURCE_AUTOMATION=1` 才静改 `sources.json`，否则仅写 `source_actions.json` 建议（防误改生产）。

---

## 5. 按 85 分，可继续提升的空间（提分点）
| 方向 | 现状 | 提分 | 说明 |
|---|---|---|---|
| 单条二次发布（republish） | 未落地 | +3~5 | 内容资产化/SEO 被动流量，最大短板 |
| 影子打分→主动拦截 | SHADOW 已埋点，默认关 | +1~2 | `score.py` 静默≥14次+无实质率≤5% 才升级，质量护栏 |
| 模型路由多样性 | 仅璇玑主 + Gemini 镜像兜底 | +1~2 | 接更多免费/低成本模型做兜底/择优 |
| 端点注释对齐 + 成本文案明确（估算 vs 实际¥0） | P2 待做 | +1 | 文档一致性 |
| 季度校准（八类共性） | 已写文档未跑 | +1 | 阈值随季节微调 |
| 轻前端/独立阅读页 | 仅 WP 渲染 | +1~2 | 对比商业雷达的 UI 差距 |
| discovery 多样性监控指标 | 无 | +0.5 | 观测自动发现源结构健康度 |

**若做完「单条发布 + 主动拦截 + 路由多样性」三件，可到 ~92–94/100**（仍零成本）。

---

## 6. 排版原型验证（你第 5 问）
- `publish_wp.py` 的 `render()` 自提交 `3e8382b`（RENDERER_VERSION 5→6）后，**gh-opt / feat/credit-opml 等本轮改动均未触碰该文件** → 排版与线上**一致、无变化**。
- 已离线（无网络）渲染样例日报：`/tmp/fuyr_layout_proto.html`，HTML 含 `<!-- dr-renderer:6 -->` 标记，15.6KB。
- 排版规范（v4）：卡片式、圆角阴影、三模块（项目机会库/增长运营/观点心法）、字段标签色点、变现绿色高亮、副业视角/总结暖底左边条、纯 inline style 对 wpautop 免疫、clamp() 响应式。

---
*注记：本文纠正了"单条自动发布已落地"的误解——当前代码无此能力，仅规划。若需落地，请确认后我重建 `republish_items.py` 并接线（默认关）。*
