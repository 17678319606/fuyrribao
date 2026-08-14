#!/usr/bin/env bash
# =============================================================================
# 副业日报 · 宝塔侧主触发器（每日 19:00 由宝塔计划任务调用）
# -----------------------------------------------------------------------------
# 职责：
#   1. 调 GitHub Dispatch API 触发 Actions 跑「采集 → AI → 发布」
#   2. 轮询运行结果
#   3. 失败同时告警：企业微信机器人 + 邮箱 weixinkaifa@jinbufenzi.work
# 防重复（三层兜底，绝不重复发文）：
#   ① 运行锁 flock 防止并发/重复触发
#   ② 今日已有成功运行则跳过 dispatch（省 GitHub 额度）
#   ③ GitHub 侧 preflight 再查 WP 去重（即便 ②③ 失效也不双发）
# 稳定/免费/准点：
#   - 触发器在本机（不会被 GitHub 自动销毁），且每日 dispatch 让仓库保持活跃，
#     GitHub 自带定时不会被 60 天无活跃停用
#   - 公开仓库 Actions 无限分钟，不超免费额度
# =============================================================================
set -u

REPO="17678319606/fuyrribao"
BRANCH="main"
WF="副业日报每日生成"          # 与工作流 name 保持一致
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAT_FILE="$SCRIPT_DIR/.gh_pat"
LOCK="$SCRIPT_DIR/.trigger.lock"
LOG_FILE="$SCRIPT_DIR/cron.log"

WECOM_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=ca5e918f-8e98-4c96-9734-8e5d27b298d0"
ALERT_EMAIL="weixinkaifa@jinbufenzi.work"

# ↓↓↓ 邮件 SMTP 配置：按你的服务器填写；全部留空则仅用企业微信告警 ↓↓↓
SMTP_HOST=""          # 例如 smtp.exmail.qq.com / 你的域名邮件服务器
SMTP_PORT="465"
SMTP_USER=""          # 发件账号（可与收件不同）
SMTP_PASS=""
SMTP_TLS="1"          # 1=SSL(465)  0=STARTTLS/普通(25/587)

export TZ=Asia/Shanghai

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG_FILE"; }

# ---------- 告警函数 ----------
send_wecom() {
  local msg="$1"
  python3 - "$WECOM_URL" "$msg" <<'PY' 2>/dev/null
import sys, json, urllib.request
url, msg = sys.argv[1:3]
data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
req = urllib.request.Request(url, data=data,
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=10)
except Exception as e:
    print("WECOM_FAIL", e)
PY
}

send_email() {
  local subj="$1" body="$2"
  [ -z "$SMTP_HOST" ] && { log "⚠️ 未配置 SMTP，跳过邮件告警"; return; }
  python3 - "$SMTP_HOST" "$SMTP_PORT" "$SMTP_USER" "$SMTP_PASS" "$SMTP_TLS" \
              "$ALERT_EMAIL" "$subj" "$body" <<'PY' 2>/dev/null
import sys, smtplib, ssl
from email.message import EmailMessage
host, port, user, pw, tls, to, subj, body = sys.argv[1:9]
port = int(port); tls = (tls == "1")
msg = EmailMessage()
msg["Subject"] = subj
msg["From"] = (user or to)
msg["To"] = to
msg.set_content(body)
try:
    if tls:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user: s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            if user: s.login(user, pw)
            s.send_message(msg)
    print("EMAIL_OK")
except Exception as e:
    print("EMAIL_FAIL", e)
PY
}

alert() {
  local msg="$1"
  log "🔔 告警: $msg"
  send_wecom "$msg"
  send_email "【副业日报】生成失败" "$msg"
}

# ---------- 防并发锁 ----------
exec 9>"$LOCK" || { log "无法创建锁文件"; exit 1; }
flock -n 9 || { log "已有实例在运行，退出避免重复。"; exit 0; }

# ---------- GitHub PAT ----------
[ -f "$PAT_FILE" ] || { alert "未找到 GitHub PAT 文件: $PAT_FILE"; exit 1; }
PAT="$(tr -d '[:space:]' < "$PAT_FILE")"
[ -n "$PAT" ] || { alert "GitHub PAT 为空，请检查 $PAT_FILE"; exit 1; }
AUTH="Authorization: Bearer $PAT"
API="https://api.github.com/repos/$REPO"

# ---------- 今日已成功则跳过（防重复 dispatch） ----------
TODAY=$(date +%F)
SINCE=$(date -d "$TODAY 00:00:00" -u +%Y-%m-%dT%H:%M:%SZ)
already=$(curl -s -H "$AUTH" "$API/actions/runs?per_page=50&conclusion=success" \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print('0'); sys.exit()
since = '$SINCE'
for r in d.get('workflow_runs', []):
    if r.get('created_at', '') >= since:
        print('1'); break
else:
    print('0')
" 2>/dev/null)
if [ "$already" = "1" ]; then
  log "今日已有成功运行，跳过 dispatch（防重复）。"
  exit 0
fi

# ---------- 触发 workflow ----------
log "触发 workflow: $WF @ $BRANCH"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "$AUTH" -H "Accept: application/vnd.github+json" \
  "$API/actions/workflows/daily-report.yml/dispatches" \
  -d "{\"ref\":\"$BRANCH\"}")
if [ "$HTTP" != "204" ]; then
  alert "GitHub dispatch 失败（HTTP $HTTP）。请检查 PAT 权限/配额/网络。"
  exit 1
fi
log "dispatch 成功，开始轮询运行结果…"

# ---------- 轮询结果（最长约 20 分钟） ----------
for i in $(seq 1 40); do
  sleep 30
  RUN=$(curl -s -H "$AUTH" "$API/actions/runs?per_page=5" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(''); sys.exit()
for r in d.get('workflow_runs', []):
    if r.get('name') == '$WF':
        print(r['id'], r['status'], (r.get('conclusion') or ''))
        break
" 2>/dev/null)
  [ -z "$RUN" ] && { log "尚未查到运行，继续…"; continue; }
  RID=$(echo "$RUN" | awk '{print $1}')
  ST=$(echo "$RUN" | awk '{print $2}')
  CON=$(echo "$RUN" | awk '{print $3}')
  if [ "$ST" = "completed" ]; then
    if [ "$CON" = "success" ]; then
      log "✅ 运行 $RID 成功，日报已发布。"
      send_wecom "【副业日报】$TODAY 日报已成功发布 ✅\nhttps://github.com/$REPO/actions/runs/$RID"
      exit 0
    else
      alert "【副业日报】$TODAY 运行 $RID 失败（conclusion=$CON）。\n日志: https://github.com/$REPO/actions/runs/$RID"
      exit 1
    fi
  fi
  log "运行中（$ST）… 第 $i 次检查"
done
alert "【副业日报】轮询超时（>20 分钟未结束），请手动检查。\nhttps://github.com/$REPO/actions/runs"
exit 1
