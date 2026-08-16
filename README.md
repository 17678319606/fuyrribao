# 副业日报（ai-sidehustle-report）· 方案设计说明

> 状态：已落地运行（GitHub Actions + 宝塔双触发），现进入「持续优化配置刷新」阶段
> 目标：全自动 —— 北京时间每天 19:00（宝塔主触发）/ 19:40·20:10（GitHub 备用 cron）采集 → AI 生成日报 → 直接发布到 WordPress「日报」类目

---

## 一、可行性结论：**可行** ✅

你最担心的"国外源国内访问不了"恰恰是这套方案的**核心优势来源**：

| 痛点 | 解决方式 |
|------|----------|
| 国外源（Product Hunt / IndieHackers / Reddit / App Store 海外区等）在你国内服务器上抓不到 | **抓取放在 GitHub Actions Runner 上跑**。Runner 部署在境外，出网不受限，能直连这些源 |
| 每天只拿增量、不重复推旧内容 | 仓库内维护 `state/seen.json`（已见 ID 集合），每次运行前过滤，运行后追加 |
| AI 结构化 + WordPress 发布 | Runner 同时能访问 `ai.jinbufenzi.com` 与 `dajiayouxuan.com` REST API，三步全在一条流水线内完成 |

也就是说：**你的 WordPress 服务器（国内）完全不参与抓取**，只作为最终发布端点；重活全在 GitHub 免费 Runner 上完成。这正是你设想的"AI 在 GitHub 处理完通过 REST API 直接发布"的落地形态。

---

## 二、免费额度测算（GitHub Free）

- **Actions 时长**：私有仓库每月 2,000 分钟；公开仓库**无限**。
  本流水线每天跑 1 次，单次约 3–6 分钟 → 每月约 90–180 分钟，**远低于 2,000 上限**。
- **GitHub API**：鉴权后 5,000 次/小时。本方案每天仅几十次调用（读 1c7 仓库 diff + 提交）。充裕。
- **仓库存储**：仅存 JSON 文本 + 去重状态，体积可忽略（软上限 1GB/仓库）。
- **定时任务稳定性**：`cron` 在高负载时可能延迟几分钟（非秒级精确）。
  且 GitHub 会在仓库 **60 天无提交**后停用定时——而本流水线每天都会提交状态文件，
  仓库每天有活动，因此**不会被停用**。

---

## 三、端到端架构

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions Runner（境外，出网自由）                       │
│                                                               │
│  步骤1 fetch_signals.py                                        │
│    ├─ radar-dashboard 19源逻辑（PH/IH/Reddit/AppStore/GitHub…） │
│    ├─ 中年指南 worker（HTML 解析，取 阅读原文 链接+时间）        │
│    └─ 1c7 中文独立开发者（GitHub API 取当日新增 commit）         │
│        ↓ 与 state/seen.json 比对 → 仅保留增量                  │
│        ↓ data/candidates-YYYY-MM-DD.json                      │
│                                                               │
│  步骤2 generate_report.py                                      │
│    └─ 默认走璇玑国内网关 ai.jinbufenzi.com（主通道）；仅当显式配置       │
│       ai_base_url 指向 Gemini 时才用 Gemini 兜底（非默认首选）        │
│       喂 SKILL.md 规则 + candidates → 结构化日报 JSON           │
│        ↓ data/report-YYYY-MM-DD.json                          │
│                                                               │
│  步骤3 publish_wp.py                                           │
│    └─ WP REST API（app password 鉴权）POST /wp/v2/posts        │
│       status=publish，归类「日报」 → 直接发布（非草稿）         │
│                                                               │
│  步骤4 git commit state/ + data/ 回仓库（自带 GITHUB_TOKEN）    │
└─────────────────────────────────────────────────────────────┘
```

### 三个脚本职责（落地时实现）
1. **fetch_signals.py**：多源采集 + 增量去重。
   - radar-dashboard 的 `scripts/daily_signals.py` 已封装 19 源，直接复用/改写即可；
   - 中年指南 worker 用 `requests` + `BeautifulSoup` 解析 HTML，按 `/posts/<id>` 去重；
   - 1c7 仓库用 GitHub API 取最近 1 天 commit 的 diff，提取新增项目条目。
2. **generate_report.py**：读取 candidates，调用 AI（SKILL.md 规则），解析并校验返回的 JSON，
   写 `report-*.json`。含失败重试（2 次）+ 超时保护。
3. **publish_wp.py**：把 report JSON 渲染成 WP 文章 HTML（三模块 + 每日总结），
   解析/创建「日报」类目 ID，POST 发布。含标题去重（同日不重复发）。

---

## 四、数据安全方案（重要）

- **绝不硬编码任何密钥**。AI Key、WP 应用密码只以 `${{ secrets.X }}` 形式出现在工作流里。
- **不滥用你的 GitHub PAT**：流水线提交回仓库用的是 GitHub **自带的 `GITHUB_TOKEN`**，
  不需要把你给的 PAT 放进仓库。你给的 PAT 仅用于首次把本方案推送到 `fuyrribao` 仓库。
- 需要在仓库 `Settings → Secrets and variables → Actions` 里配置（**必填两项 + 一项备用，其余可选**）：
  - `AI_SIDEHUSTLE_API_KEY`（**必填·默认主 AI**）：璇玑国内网关 key，走 `ai.jinbufenzi.com/v1`。代码默认主通道就是它（无显式 `ai_base_url` 时）。
  - `WP_APP_PASSWORD`（**必填·发布**）：WordPress 应用密码（App Password）。WP 登录用户名已在工作流 `env.WP_USER` 硬编码为 `tougao`，**无需单独配置**。
  - `GEMINI_API_KEY`（**可选·仅显式启用 Gemini 时**）：只有当你显式设置 `ai_base_url` 指向 Gemini 原生 API，才需要它作兜底；不设置则**不参与**主流程（打璇玑必失败）。
  - 可选：`AI_MODEL`（覆盖默认模型）、`ai_base_url`/`AI_BASE_URL`（显式切换 Gemini 等端点）、`AI_BASE_URL_POOL`（同构镜像池）、`AI_PROXY_POOL`、`AI_REQUEST_TIMEOUT`、`AI_FORCE_NON_STREAM`。
- 📌 **AI 路由现状**：默认璇玑国内网关（ai.jinbufenzi.com）为主通道生成日报；仅当显式配置 `ai_base_url` 指向 Gemini 时才用 Gemini 兜底。二者**非**「Gemini 首选」，请勿混淆。WordPress 发布独立走 WP REST API，互不阻塞。
- ⚠️ **聊天里已明文出现的密钥有泄露风险**：GitHub PAT、AI Key、WP 应用密码都曾粘贴在对话中。
  建议方案上线后到对应平台**重置/吊销**这三个凭证，再重新生成新值填入 Secrets。

---

## 五、部署步骤（确认后执行）
1. 把本目录内容推送到 `github.com/17678319606/fuyrribao`（路径：根目录放 `SKILL.md`、
   `.github/workflows/daily-report.yml`、`scripts/`、`state/seen.json` 初始空文件）。
2. 仓库 Settings 按上文配置 `GEMINI_API_KEY` / `WP_APP_PASSWORD` / `AI_SIDEHUSTLE_API_KEY` 三个 Secrets（其余可选）。
3. 手动触发一次 `workflow_dispatch` 验证全链路；确认无误后坐等每天 18:00 自动出报。
4. 上线后轮换泄露过的三个密钥。

---
交付物清单：
- `SKILL.md`：JSON schema + 生成 prompt（AI 照填字段）
- `.github/workflows/daily-report.yml`：三步流水线
- `README.md`：本设计说明
- （设计团队产出）`preview/daily-report-preview.html`：日报文章排版原型供你确认结构
