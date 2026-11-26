#!/usr/bin/env bash
# 基础自检：一条命令回答"现在能不能用"。
#
# 只跑快的东西（约 1 分钟）。后端全量单测要 18 分钟，不放在这里——
# 它该在 CI 或改完代码后单独跑，不该挡住每次自检。
#
#   ./check.sh               跑全部（含界面验收 journey）
#   ./check.sh --no-smoke    只跑静态检查 + 界面验收（不需要后端在跑）
#   ./check.sh --no-acceptance  跳过界面验收 journey
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
NODE="${BLANK_NODE:-E:/env/node/node-v24.20.0-win-x64/node.exe}"
PY="${BLANK_PY:-$ROOT/services/api/.venv/Scripts/python.exe}"
ACCEPT_JOURNEY="${BLANK_ACCEPTANCE_JOURNEY:-$ROOT/acceptance/kanban.json}"
ACCEPT_PAGE="${BLANK_ACCEPTANCE_PAGE:-$ROOT/acceptance/fixtures/kanban.html}"
FAILED=0
SKIP_SMOKE=0
SKIP_ACCEPTANCE=0
for arg in "$@"; do
  case "$arg" in
    --no-smoke) SKIP_SMOKE=1 ;;
    --no-acceptance) SKIP_ACCEPTANCE=1 ;;
  esac
done

pass() { printf '[PASS] %s\n' "$1"; }
fail() { printf '[FAIL] %s  %s\n' "$1" "${2:-}"; FAILED=$((FAILED + 1)); }

echo "== 前端类型"
if ( cd "$ROOT/web" && NODE_OPTIONS= "$NODE" ./node_modules/typescript/bin/tsc --noEmit ) 2>/tmp/check-tsc.log; then
  pass "tsc --noEmit"
else
  fail "tsc --noEmit" "$(head -3 /tmp/check-tsc.log | tr '\n' ' ')"
fi

echo
echo "== 前端 Lint"
if ( cd "$ROOT/web" && NODE_OPTIONS= "$NODE" ./node_modules/eslint/bin/eslint.js src --max-warnings=0 ) >/tmp/check-eslint.log 2>&1; then
  pass "eslint src"
else
  fail "eslint src" "$(tail -3 /tmp/check-eslint.log | tr '\n' ' ')"
  echo "      详见 /tmp/check-eslint.log"
fi

echo
echo "== 前端单测"
if ( cd "$ROOT/web" && NODE_OPTIONS= "$NODE" --test --test-concurrency=2 --import ./test-bootstrap.mjs "src/**/*.test.ts" scripts/csp.test.mjs ) >/tmp/check-node.log 2>&1; then
  pass "node --test  $(grep -E '^ℹ pass' /tmp/check-node.log | tr -d '\r')"
else
  fail "node --test" "$(grep -E '^ℹ (pass|fail)' /tmp/check-node.log | tr '\n' ' ')"
fi

if [ "$SKIP_SMOKE" = "1" ]; then
  echo
  echo "== 端到端冒烟（已跳过）"
else
  echo
  echo "== 端到端冒烟"
  if "$PY" "$ROOT/services/api/scripts/smoke_flow.py" --timeout 150 >/tmp/check-smoke.log 2>&1; then
    pass "建项目→发一句→模型回→事件链完整  $(grep -E '^[0-9]+/[0-9]+ 通过' /tmp/check-smoke.log | tr -d '\r')"
  else
    fail "端到端冒烟" "$(grep -E '^\[FAIL\]|^[0-9]+/[0-9]+ 通过' /tmp/check-smoke.log | head -3 | tr '\n' ' ')"
    echo "      详见 /tmp/check-smoke.log"
  fi
fi

echo
echo "== 执行式界面验收"
if [ "$SKIP_ACCEPTANCE" = "1" ]; then
  echo "[SKIP] journey（--no-acceptance）"
elif PYTHONUTF8=1 "$PY" "$ROOT/scripts/acceptance.py" --journey "$ACCEPT_JOURNEY" --page "$ACCEPT_PAGE" >/tmp/check-acceptance.log 2>&1; then
  pass "journey 首屏 + 动作 + 刷新  $(sed -n 's/^结论：//p' /tmp/check-acceptance.log | tail -1)"
else
  fail "执行式界面验收" "$(sed -n 's/^结论：//p' /tmp/check-acceptance.log | tail -1)"
  echo "      详见 /tmp/check-acceptance.log"
fi

echo
if [ "$FAILED" = "0" ]; then
  echo "全部通过。"
  exit 0
fi
echo "$FAILED 项未通过。"
exit 1
