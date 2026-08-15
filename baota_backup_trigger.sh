#!/bin/bash
# ============================================================
# 副业日报 · 宝塔计划任务兜底脚本
# 用途：在 GitHub 定时触发失败/漏跑时，通过 API 手动触发一次
# 触发方式：宝塔「计划任务」→ Shell脚本 → 每天 19:15 执行
# ============================================================

# ---------- 配置区（按你的实际情况修改）----------
GITHUB_PAT="<YOUR_GITHUB_PAT_HERE>"  # 替换为你的 GitHub Personal Access Token（需要 repo 权限）
REPO_OWNER="17678319606"
REPO_NAME="fuyrribao"
WORKFLOW="daily-report.yml"
BRANCH="main"

# 日志文件
LOG_FILE="/tmp/fuyrribao_bt_cron.log"

# ---------- 执行 ----------
echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 宝塔兜底触发开始 ===" >> "$LOG_FILE"

# 先检查今天是否已经成功运行过（避免重复触发）
LAST_RUN=$(curl -sS -m 20 \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/runs?per_page=5" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
today = __import__('datetime').datetime.now(__import__('datetime').timezone(__import__('datetime').timedelta(hours=8))).strftime('%Y-%m-%d')
for r in d.get('workflow_runs', []):
    if r['created_at'].startswith(today) and r['name'] == '副业日报每日生成':
        print(r['id'], r['status'], r['conclusion'] or 'running')
        break
else:
    print('NONE')
" 2>/dev/null)

echo "今日最近运行: $LAST_RUN" >> "$LOG_FILE"

# 判断是否需要触发
RUN_ID=$(echo "$LAST_RUN" | awk '{print $1}')
RUN_STATUS=$(echo "$LAST_RUN" | awk '{print $2}')
RUN_CONCLUSION=$(echo "$LAST_RUN" | awk '{print $3}')

if [ "$RUN_ID" != "NONE" ] && [ "$RUN_CONCLUSION" = "success" ]; then
  echo "✅ 今日 GitHub 已成功运行过(ID=$RUN_ID)，跳过兜底触发" >> "$LOG_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 兜底结束（已成功，跳过）===" >> "$LOG_FILE"
  exit 0
fi

if [ "$RUN_ID" != "NONE" ] && [ "$RUN_STATUS" = "in_progress" ]; then
  echo "⏳ GitHub 正在运行中(ID=$RUN_ID)，等待完成而非重复触发" >> "$LOG_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 兜底结束（运行中）===" >> "$LOG_FILE"
  exit 0
fi

# 发起触发
HTTP_CODE=$(curl -sS -m 25 -o /tmp/fuyr_dispatch_resp.txt -w "%{http_code}" -X POST \
  -H "Authorization: Bearer $GITHUB_PAT" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW}/dispatches" \
  -d "{\"ref\":\"${BRANCH}\",\"inputs\":{\"force\":\"true\"}}")

echo "触发结果: HTTP=$HTTP_CODE" >> "$LOG_FILE"
cat /tmp/fuyr_dispatch_resp.txt >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

if [ "$HTTP_CODE" = "204" ]; then
  echo "✅ 兜底触发成功（force=true，将强制更新 WP 文章）" >> "$LOG_FILE"
else
  echo "❌ 兜底触发失败(HTTP=$HTTP_CODE)" >> "$LOG_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 宝塔兜底触发结束 ===" >> "$LOG_FILE"
