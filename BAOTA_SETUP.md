# 宝塔计划任务配置说明（副业日报 · 主动触发为主）

> 作用：主动触发以**宝塔计划任务**为主。服务器上的 `trigger_daily.sh` 会调用 GitHub Dispatch API，
> 让仓库的 Actions 跑完「采集 → AI 生成 → 发布 WordPress」全流程，并在失败时自动发**企业微信机器人**告警。
> GitHub 自带的 19:40 / 20:10 cron 仅作**备用兜底**，无需你管。

---

## 一、前置条件（先确认）
1. 宝塔服务器能访问外网（至少能通 `api.github.com`）。
2. 服务器已装：`git`、`python3`（3.8+）、`curl`（宝塔默认都有，不用另装）。
3. 你手上有 **GitHub 永久 PAT**，且具备 `repo` + `workflow` 权限（用来调 Dispatch API）。

---

## 二、步骤（照着点就行）

### 第 1 步：把仓库放到服务器
- 宝塔面板 → 文件，挑一个目录（如 `/www/wwwroot/`），点「终端」或 SSH 登录后执行：
  ```bash
  git clone https://github.com/17678319606/fuyrribao.git /www/wwwroot/fuyrribao
  ```
- 如果以前已经放过，进目录拉一下最新：
  ```bash
  cd /www/wwwroot/fuyrribao && git pull
  ```

### 第 2 步：写入 GitHub 令牌（关键）
- 在仓库目录里建一个文件，名叫 **`.gh_pat`**（注意前面有点），内容只有一行：你的 GitHub 永久 PAT，**不要换行、不要多余空格**。
- 方式 A（宝塔文件管理器）：进 `fuyrribao` 目录 → 新建文件 `.gh_pat` → 粘贴令牌 → 保存。
- 方式 B（SSH 终端）：
  ```bash
  printf '%s' '这里替换成你的GitHub永久PAT' > /www/wwwroot/fuyrribao/.gh_pat
  ```
  > ⚠️ 上面命令里的 `这里替换成你的GitHub永久PAT` 要换成你真正的令牌；不要在文件里留任何换行。

### 第 3 步：建宝塔计划任务
1. 宝塔面板 → **计划任务** → **添加任务**。
2. 任务类型：选 **Shell 脚本**。
3. 任务名称：`副业日报每日触发`（随便起）。
4. 执行周期：选 **每天**，时间填 **19 时 0 分**（即北京时间 19:00 主动触发）。
5. 脚本内容（把路径换成你自己的）：
   ```bash
   bash /www/wwwroot/fuyrribao/trigger_daily.sh
   ```
6. 点 **保存**。

### 第 4 步：先手动测一次
- 在计划任务列表里，刚建的任务右边点 **立即执行**，再看 **日志**；
- 或 SSH 直接跑：
  ```bash
  bash /www/wwwroot/fuyrribao/trigger_daily.sh
  ```
- 运行细节会写到 `fuyrribao/cron.log`，出问题时看它。

### 第 5 步：确认告警已接好
- 脚本内置了企业微信机器人（webhook 已在脚本里写好），**失败会自动推送**到你的企业微信。
- 你不用再配机器人；只要机器人 key 没变就一直生效。

---

## 三、不会重复发文章的几道防线
1. 脚本用 `flock` 文件锁，同一时刻只跑一个，避免并发重复触发。
2. 今天已经成功跑过，会跳过这次 dispatch（省 GitHub 额度）。
3. 即便 dispatch 了，GitHub 侧发布时会按「今天日期」去重，**同一天只覆盖更新同一篇文章，绝不发第二篇**。

---

## 四、常见排错
| 现象 | 原因 / 处理 |
|------|------|
| 日志报「未找到 GitHub PAT 文件」 | `.gh_pat` 没建、或路径不对、或权限不足。重新第 2 步。 |
| Dispatch 返回 401/403 | PAT 失效或权限不够（要 `repo`+`workflow`）。去 GitHub 重新生成带这两个权限的令牌，覆盖 `.gh_pat`。 |
| 日志显示「今日文章已存在，跳过 dispatch」 | 正常！说明 GitHub 侧（或备用 cron）今天已经生成过，宝塔主动跳过，不双发。 |
| 企业微信没收到告警 | 检查 `trigger_daily.sh` 里的 `WECOM_URL` 是否为你的机器人 key；网络能否访问 `qyapi.weixin.qq.com`。 |

---

## 五、安全提醒
聊天里明文出现过的令牌、AI Key、WP 应用密码都视为**已泄露**。等日报稳定跑几天后，
建议去对应平台**重置/重新生成**这些凭证，再更新到 GitHub Secrets 和服务器 `.gh_pat`。
