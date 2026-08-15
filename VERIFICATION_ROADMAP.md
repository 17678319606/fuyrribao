# 副业日报自动化 · 验证与推进路线图

> 按你的要求（先看能否跑通 AI 生产 → 再扩源 → 再优化排版 → 再测 WP 发布 → 稳定 → 纳定时），
> 把整体推进顺序固化成这份可执行的检查单。所有改动都已写入 git，可随时 `git revert` 单条回滚。

---

## 一、本轮已完成的改动（已落盘，待推送/已推送）
| 文件 | 改动 | 对应你的要求 |
|------|------|------|
| `SKILL.md` | 日报条数区间 **15–30 → 0–50**；明确每源单日上限 100、单日候选 600、AI 分批筛选逻辑 | 点 1 |
| `scripts/generate_report.py` | `SCREEN_FINAL_CAP` 35→**50**、`MAX_OUTPUT_TOKENS` 8000→**12000**，让 AI 真能产出到 50 条 | 点 1 |
| `README.md` | 刷新密钥清单（Gemini 免费**主** / 国产 AI **备** / WP 发布），更正状态与触发时间 | 点 6 |
| `.github/workflows/daily-report.yml` | `AI_ENABLE_FALLBACK` 标为 `'1'`（语义标记；备用实际由 `AI_FALLBACK_URL` 存在即启用） | 点 6 |
| `BAOTA_SETUP.md`（新） | 宝塔计划任务配置说明（白话/点按式） | 点 4 |
| `VERIFICATION_ROADMAP.md`（新） | 本路线图 | 点 9 |

> 运行时参数早已满足点 1：`common.MAX_PER_SOURCE=100`、`MAX_CANDIDATES=600`、分批筛选（`_screen_signals_with_ai`）原本就在。
> 同日 upsert（点 2）、失败告警企业微信（点 5）、空日报干跑不发布（点 7）**原本已实现**，本轮仅补充文档说明。

---

## 二、验证顺序（严格按你定的 6 步）

### 第 1 步 · 先跑通 AI 生产内容流程
- 触发方式：宝塔 19:00 自动跑，或在 GitHub 点 **Actions → 副业日报每日生成 → Run workflow**（可勾 force 覆盖）。
- 查什么：Actions 日志里 `generate_report.py` 是否成功产出 `data/report-YYYY-MM-DD.json`，且 `_meta` 显示 `model` 为 Gemini（主通道）；
  确认**不是**降级/空报告（空报告会被 `publish_wp.py` 拒发）。
- 重点验证：Gemini 免费层可用；若 Gemini 偶发失败，是否自动退回国产 AI 备用并仍出报告。

### 第 2 步 · 再拓展信息源
- 在 `scripts/sources.json` 加稳定中文源（少数派 / 即刻 / 知乎圈子 / 优质 RSS 等），观察 `candidates-*.json` 数量上升。
- 查什么：`fetch_signals.py` 是否每源 ≤100、总量护栏生效；日报条数是否从今天的 6 条向 **15–30–50** 靠拢。
- 注意：源多 → 候选多 → AI 分批调用变多，单日耗时上升仍应在 120min 超时内。

### 第 3 步 · 优化卡片式排版原型（阅读体验）
- `preview/daily-report-preview.html` 已是 v3 卡片式（圆角/阴影/字段隐藏空值/移动端响应式）。
- 在浏览器打开核对：移动端（≤480px）是否不溢出、空字段是否真的不显示、每日总结黄色卡片是否清晰。
- 按需微调配色 / 字号 / 间距，改 `publish_wp.py` 里的 `CSS` 常量后，用 `workflow_dispatch` + force 重发验证。

### 第 4 步 · 测试 WordPress 日报发布
- 确认 `publish_wp.py`：**同日已有文章 → 覆盖更新（链接不变），绝不新建第二篇**；空日报 / `ai_failed` → 不发布；残缺文章 → broken 自愈覆盖。
- 去 WP 后台「日报」类目看文章是否正常渲染、类目正确、无可见裸 URL。

### 第 5 步 · 整个流程稳当跑通
- 连续观察 **3–5 天**：成功率、是否触发过企业微信告警、单日耗时是否 < 120min、cron.log 有无异常。

### 第 6 步 · 最后纳入定时任务
- 宝塔 19:00 主触发 + GitHub 19:40/20:10 备用 cron **已就位**；心跳提交已防 GitHub 60 天停用。
- 确认无误后即可长期无人值守运行。

---

## 三、安全提醒（务必做）
聊天里明文出现过的 **GitHub PAT / AI Key / WP 应用密码** 一律视为已泄露。
等日报稳定后，到对应平台**重置/重新生成**这些凭证，再更新 GitHub Secrets 与服务器 `.gh_pat`。

---

## 四、回滚方式
所有改动都在 git 历史里。若某次改动导致异常：
```bash
cd /www/wwwroot/fuyrribao && git log --oneline   # 找到对应提交
git revert <提交号>                                # 单条回滚，不丢其他改动
```
