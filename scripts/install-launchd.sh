#!/bin/sh
# Keep one company crawl resident under launchd on the crawl host (Macmini by default).
#
#   scripts/install-launchd.sh install   nvidia   # write + load ~/Library/LaunchAgents/ai.inresearch.fetchspec.nvidia.plist
#   scripts/install-launchd.sh uninstall nvidia   # unload + remove the agent (data and ledger untouched)
#
# launchd relaunches the worker after a crash, a remote-error pause or a reboot
# (ThrottleInterval spaces retries); it stays down after `run-company.sh stop`
# or once the frontier is exhausted, because those exit 0.
# FETCHSPEC_DATA_ROOT / FETCHSPEC_STATE_ROOT in the environment are baked into the plist.
set -eu

action=${1:-}; company=${2:-}
[ -n "$action" ] && [ -n "$company" ] || { sed -n '2,5p' "$0"; exit 64; }

repo=$(cd "$(dirname "$0")/.." && pwd)
label=ai.inresearch.fetchspec.$company
plist=$HOME/Library/LaunchAgents/$label.plist
state_root=${FETCHSPEC_STATE_ROOT:-$HOME/.local/state/fetchspec}
domain=gui/$(id -u)

case $action in
  uninstall)
    launchctl bootout "$domain/$label" 2>/dev/null || true
    rm -f "$plist"; echo "removed $label" ;;
  install)
    mkdir -p "$state_root/$company" "$(dirname "$plist")"
    env_block="<key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>"
    [ -n "${FETCHSPEC_DATA_ROOT:-}" ] && env_block="$env_block<key>FETCHSPEC_DATA_ROOT</key><string>$FETCHSPEC_DATA_ROOT</string>"
    [ -n "${FETCHSPEC_STATE_ROOT:-}" ] && env_block="$env_block<key>FETCHSPEC_STATE_ROOT</key><string>$FETCHSPEC_STATE_ROOT</string>"
    cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$repo/scripts/run-company.sh</string><string>run</string><string>$company</string></array>
  <key>WorkingDirectory</key><string>$repo</string>
  <key>EnvironmentVariables</key><dict>$env_block</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>600</integer>
  <key>StandardOutPath</key><string>$state_root/$company/launchd.log</string>
  <key>StandardErrorPath</key><string>$state_root/$company/launchd.log</string>
</dict>
</plist>
PLIST
    plutil -lint "$plist" >/dev/null
    launchctl bootout "$domain/$label" 2>/dev/null || true
    launchctl bootstrap "$domain" "$plist"
    echo "loaded $label -> $plist"
    echo "log: $state_root/$company/launchd.log" ;;
  *) echo "unknown action: $action"; exit 64 ;;
esac
