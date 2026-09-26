#!/bin/sh
# Operate one company-level crawl without hand-built environment variables.
#
#   scripts/run-company.sh start  nvidia [company-crawl args...]   # background, resumable
#   scripts/run-company.sh run    nvidia [company-crawl args...]   # foreground (launchd uses this)
#   scripts/run-company.sh status nvidia                           # pace, ETA, errors
#   scripts/run-company.sh stop   nvidia                           # graceful: STOP file, worker exits after current request
#   scripts/run-company.sh log    nvidia                           # follow the latest log
#
# Data root precedence matches the CLI: FETCHSPEC_DATA_ROOT > config/archive.local.json > ~/.local/share/fetchspec.
# Logs go to ${FETCHSPEC_STATE_ROOT:-~/.local/state/fetchspec}/<company>/.
set -eu

action=${1:-}; company=${2:-}
if [ -z "$action" ] || [ -z "$company" ]; then
  sed -n '2,11p' "$0"; exit 64
fi
shift 2

repo=$(cd "$(dirname "$0")/.." && pwd)
python=${FETCHSPEC_PYTHON:-python3}
state_root=${FETCHSPEC_STATE_ROOT:-$HOME/.local/state/fetchspec}
log_dir=$state_root/$company
export PYTHONPATH="$repo/src" PYTHONUNBUFFERED=1

data_root=$(cd "$repo" && "$python" -m fetchspec where)
ledger=$data_root/ledger/companies/$company

cli() { (cd "$repo" && "$python" -m fetchspec "$@"); }

case $action in
  status)
    cli company-status --company "$company" --summary "$@" ;;
  stop)
    mkdir -p "$ledger"; touch "$ledger/STOP"
    echo "STOP requested: $ledger/STOP (worker exits after the current request)" ;;
  log)
    latest=$(ls -t "$log_dir"/*.log 2>/dev/null | head -1)
    [ -n "$latest" ] || { echo "no logs in $log_dir"; exit 1; }
    exec tail -f "$latest" ;;
  run|start)
    mkdir -p "$log_dir"
    # Check for a live worker before touching STOP: removing it first would
    # cancel a pending stop and leave the old worker running.
    if [ "$action" = start ] && cli company-status --company "$company" --summary --json | grep -q '"worker_active": true'; then
      if [ -e "$ledger/STOP" ]; then
        echo "$company worker is stopping (STOP pending); wait for worker=stopped, then start again"
      else
        echo "$company worker already running"
      fi
      exit 0
    fi
    # A STOP file left over from an earlier stop would end the new run at once.
    rm -f "$ledger/STOP"
    if [ "$action" = run ]; then
      # caffeinate keeps the Mac awake only while this worker lives.
      exec caffeinate -i "$python" -m fetchspec company-crawl --company "$company" --fetch "$@"
    fi
    log=$log_dir/$(date +%Y%m%d-%H%M%S).log
    nohup "$0" run "$company" "$@" >"$log" 2>&1 &
    echo "started $company (pid $!), data root $data_root"
    echo "log: $log" ;;
  *)
    echo "unknown action: $action"; exit 64 ;;
esac
