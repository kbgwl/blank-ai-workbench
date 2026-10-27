#!/usr/bin/env bash
# 起 #116–#119 三格点验用的隔离实例（默认 :8061）。
#
# 只带路径、不带凭据：settings.json / secret.key / run.db 全在 BLANK_REALMODEL_HOME
# 指向的目录里（默认 E:/tmp/realmodel），那个目录**永远不进仓库**。
# 不加 --with-hooks 时是**对照姿态**（不配任何钩子），对照格要用它。
#
# 同刻双实例并跑（#121）：一台带钩子、一台不带，各自的 home 必须是**独立的一份 run.db**
# （共用一份会互相写），但 secret.key 必须同一把（供应商行是它加密封存的）。
# 给第二台传 BLANK_REALMODEL_SEED_HOME=<主实例的 home>，缺库时从那里拷一份过来。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HOME_DIR="${BLANK_REALMODEL_HOME:-E:/tmp/realmodel}"
PORT="${BLANK_REALMODEL_PORT:-8061}"
PY="${BLANK_REALMODEL_PYTHON:-$REPO/services/api/.venv/Scripts/python.exe}"

export BLANK_DATABASE_URL="sqlite+aiosqlite:///$HOME_DIR/run.db"
export BLANK_SECRET_KEY_FILE="$HOME_DIR/secret.key"
export BLANK_SETTINGS_FILE="$HOME_DIR/settings.json"
export BLANK_MEMORY_DIR="$HOME_DIR/memory"
export BLANK_NOTES_DIR="$HOME_DIR/notes"
export BLANK_AUDIT_DIR="$HOME_DIR/audit"
export BLANK_WORKTREE_ROOT="$HOME_DIR/worktrees"
export BLANK_SANDBOX_ENABLED=0
export BLANK_REALMODEL_HOME="$HOME_DIR"
export PYTHONPATH=src

if [[ "${1:-}" == "--with-hooks" ]]; then
  # 钩子配置由 hooks_config.py 生成并自检：上一版在这里用 printf 手拼 JSON，
  # 引号没转义 → 产品按 fail-closed 读成「没有钩子」→ 三格白跑、钱白花。
  if ! BLANK_HOOKS="$("$PY" -X utf8 -E "$HERE/hooks_config.py" --python "$PY" --dir "$HERE/hooks" --print)"; then
    echo "[ABORT] 生成钩子配置失败" >&2
    exit 2
  fi
  if ! "$PY" -X utf8 -E "$HERE/hooks_config.py" --python "$PY" --dir "$HERE/hooks" --check; then
    echo "[ABORT] 钩子姿态自检没过，实例不起（对照格请用不带 --with-hooks 的那一种）" >&2
    exit 2
  fi
  export BLANK_HOOKS
  export BLANK_HOOK_SETTLE=2
  POSTURE=hooks
  echo "[launch] 钩子姿态：post_tool_use(async) + worktree_create + worktree_remove"
else
  POSTURE=no-hooks
  echo "[launch] 对照姿态：不配任何钩子（对照格用这一种）"
fi

# 姿态留痕：装置在花钱之前读这一份，主格/编码格要求 hooks，对照格要求 no-hooks。
# 手起的实例没有这份留痕 → 装置直接 rc 2 拒绝跑，不再拿真模型去试姿态。
mkdir -p "$HOME_DIR"
# 第二台实例得有**自己的一份 run.db**（共用一份会互相写），但 secret.key 得是同一把
# （供应商行里那枚 Key 只有同一把密钥解得开）。只拷文件，从不读内容。
if [[ -n "${BLANK_REALMODEL_SEED_HOME:-}" && ! -f "$HOME_DIR/run.db" ]]; then
  for f in run.db secret.key settings.json; do
    if [[ -f "$BLANK_REALMODEL_SEED_HOME/$f" ]]; then
      cp "$BLANK_REALMODEL_SEED_HOME/$f" "$HOME_DIR/$f"
    fi
  done
  echo "[launch] 从 $BLANK_REALMODEL_SEED_HOME 铺了 run.db/secret.key/settings.json 到 $HOME_DIR"
fi
printf '{"port": %s, "posture": "%s", "hook_settle": "%s", "started_at": "%s"}\n' \
  "$PORT" "$POSTURE" "${BLANK_HOOK_SETTLE:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$HOME_DIR/instance-posture-$PORT.json"
echo "[launch] 姿态留痕：$HOME_DIR/instance-posture-$PORT.json（posture=$POSTURE）"

cd "$REPO/services/api"
exec "$PY" -m uvicorn blank_api.app:app --host 127.0.0.1 --port "$PORT"
