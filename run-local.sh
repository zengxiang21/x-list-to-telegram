#!/bin/bash
# 本地每 5 分钟运行一次：抓 X 列表新推文 -> 推送 Telegram 频道
# 由 launchd 调用（com.zengxiang.xlist-telegram）
cd /Users/zengxiang/.zcode/workspace/default/x-list-to-telegram || exit 1

REPO_DIR="/Users/zengxiang/.zcode/workspace/default/x-list-to-telegram"
VENV="/Users/zengxiang/.zcode/workspace/default/.venv"
LOG="$REPO_DIR/local-run.log"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export GIT_TERMINAL_PROMPT=0

# 日志只保留最近 500 行
if [ -f "$LOG" ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
echo "=== $(date '+%F %T') ===" >> "$LOG"

# 读取配置
set -a
. "$REPO_DIR/local.env"
set +a

# 先同步远端状态，避免和 GitHub Actions 重复推送
HTTPS_PROXY="$PROXY" git pull --rebase --autostash -q >> "$LOG" 2>&1 || git rebase --abort >> "$LOG" 2>&1

"$VENV/bin/python" monitor.py >> "$LOG" 2>&1
rc=$?

# 把去重进度同步回 GitHub（尽力而为）
if [ -f "$REPO_DIR/state.json" ]; then
  git add -f state.json >> "$LOG" 2>&1
  git -c user.name="local-runner" -c user.email="zengxiang21@users.noreply.github.com" \
      commit -q -m "chore: local state update [skip ci]" >> "$LOG" 2>&1
  HTTPS_PROXY="$PROXY" git push -q >> "$LOG" 2>&1
fi

exit $rc
