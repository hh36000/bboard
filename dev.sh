#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
session="bboard-dev"

if tmux has-session -t "$session" 2>/dev/null; then
  echo "Dev session already exists. Attach with: tmux attach -t $session"
  exit 0
fi

for port in 6600 9900; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use. Stop its process before starting bboard."
    exit 1
  fi
done

if [[ ! -x "$project_dir/backend/.venv/bin/python" || ! -d "$project_dir/frontend/node_modules" ]]; then
  echo "Install dependencies first. See README.md."
  exit 1
fi

tmux new-session -d -s "$session" -n backend -c "$project_dir/backend"
"$project_dir/backend/.venv/bin/python" "$project_dir/scripts/local_database.py" || {
  bootstrap_status=$?
  # This invocation created the session; a failed bootstrap must allow retry.
  tmux kill-session -t "$session" 2>/dev/null || true
  exit "$bootstrap_status"
}
tmux send-keys -t "$session:backend" 'DB_MODE=local DATABASE_URL=postgresql+pg8000://bboard@127.0.0.1:55432/bboard BBOARD_SITE_URL=http://localhost:6600 .venv/bin/python -m uvicorn main:app --reload --host 127.0.0.1 --port 9900 --no-proxy-headers --no-access-log --ws-max-size 409600 --ws-max-queue 4 --ws-per-message-deflate false' Enter
tmux new-window -t "$session" -n frontend -c "$project_dir/frontend"
tmux send-keys -t "$session:frontend" 'npm run dev' Enter

echo "Frontend: http://localhost:6600"
echo "Backend:  http://localhost:9900"
echo "Logs:     tmux attach -t $session"
