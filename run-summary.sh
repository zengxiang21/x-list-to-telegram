#!/bin/bash
# 每天早上 8:50 生成 X 列表日报并发到 Telegram（由 launchd 调用）
REPO_DIR="/Users/zengxiang/.zcode/workspace/default/x-list-to-telegram"
VENV="/Users/zengxiang/.zcode/workspace/default/.venv"
LOG="$REPO_DIR/summary-run.log"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export GIT_TERMINAL_PROMPT=0

cd "$REPO_DIR" || exit 1

if [ -f "$LOG" ]; then
  tail -n 300 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
echo "=== $(date '+%F %T') ===" >> "$LOG"

set -a
. "$REPO_DIR/local.env"
set +a

# 同步远端（若 GitHub 已发过日报，state 会跳过，防止重复）
HTTPS_PROXY="$PROXY" git pull --rebase --autostash -q >> "$LOG" 2>&1

"$VENV/bin/python" summarize.py >> "$LOG" 2>&1
rc=$?

if [ -f summary-state.json ]; then
  git add summary-state.json >> "$LOG" 2>&1
  git -c user.name="local-runner" -c user.email="zengxiang21@users.noreply.github.com" \
      commit -q -m "chore: daily summary state [skip ci]" >> "$LOG" 2>&1
  HTTPS_PROXY="$PROXY" git push -q >> "$LOG" 2>&1
fi

exit $rc
