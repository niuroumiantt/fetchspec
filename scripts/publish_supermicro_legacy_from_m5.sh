#!/usr/bin/env bash
# [m5 -> macmini -> AWS] Offline archive supplement, using existing SSH aliases.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_COMMIT="$(git -C "$HERE" rev-parse HEAD)"
TASK_RUN="$(date -u +%Y%m%dT%H%M%SZ)-$$"
case "$SOURCE_COMMIT" in *[!0-9a-f]*|'') echo 'invalid source commit' >&2; exit 1;; esac

ssh mini bash -s -- "$SOURCE_COMMIT" "$TASK_RUN" <<'MINI'
set -euo pipefail
SOURCE_COMMIT="$1"
TASK_RUN="$2"
REPO="$HOME/code/fetchspec"
WORK="$HOME/.worktrees/fetchspec/legacy-catalog-${TASK_RUN}"
OUTPUT="$HOME/.local/share/fetchspec/pipeline/legacy-supermicro/${TASK_RUN}"
git -C "$REPO" fetch --quiet origin main 2>/dev/null || { echo "GitHub source synchronization failed" >&2; exit 1; }
git -C "$REPO" cat-file -e "${SOURCE_COMMIT}^{commit}"
mkdir -p "$HOME/.worktrees/fetchspec"
git -C "$REPO" worktree add --detach "$WORK" "$SOURCE_COMMIT"
cd "$WORK"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m fetchspec.legacy_catalog \
  --root "$HOME/.local/share/fetchspec" --output "$OUTPUT"
MINI

ssh inews 'set -e
sudo systemctl start inresearch-only-deploy.service
sudo cat /var/lib/inresearch-ops/inresearch-only/status
sudo cat /var/lib/inresearch-ops/inresearch-only/applied
sudo docker exec inresearch-host-inresearch-1 python3 -c "import inresearch.workflow.catalog_bundle"
'

mkdir -p "$HOME/.local/state/fetchspec"
RECEIPT="$HOME/.local/state/fetchspec/legacy-${TASK_RUN}-receipt.json"
ssh mini "cat /Users/hermes/.local/share/fetchspec/pipeline/legacy-supermicro/${TASK_RUN}/catalog-bundle.tar.gz" \
  | ssh inews 'sudo docker exec -i inresearch-host-inresearch-1 python3 -m inresearch.workflow.product_catalog import-bundle --company supermicro --input -' \
  | tee "$RECEIPT"
printf 'Receipt saved: %s\n' "$RECEIPT"
ssh inews 'set -e
curl -fsS "https://inresearch.ai/api/company-window?c=supermicro" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d[\"catalog\"].get(k) for k in (\"summary\",\"material_count\")},ensure_ascii=False,indent=2))"
'
