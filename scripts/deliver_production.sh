#!/usr/bin/env bash
# Real production delivery of one company's seeded rows, run on the macmini.
#
#   scripts/deliver_production.sh micron            # collect → package → verify → Spark receive → receipt → assignments
#   scripts/deliver_production.sh micron --check    # stop after the local verifier; nothing leaves the macmini
#
# Settings (environment, all optional):
#   INRESEARCH   clean inresearch.ai checkout on main        (default ~/code/inresearch.ai)
#   SPARK        ssh host of the production receiver         (default spark)
#   SPARK_REPO   inresearch.ai checkout on Spark             (default ~/code/inresearch.ai)
#   REVIEWER     name recorded on every parameter mapping    (default $USER)
#
# Parameter mappings are reviewed decisions; they live in scripts/mappings/<company>.tsv
# (product, table, row, cell, field, unit, condition, target) so every run maps the same cells.
set -euo pipefail

COMPANY="${1:?usage: deliver_production.sh <company> [--check]}"
MODE="${2:-}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
INRESEARCH="${INRESEARCH:-$HOME/code/inresearch.ai}"
SPARK="${SPARK:-spark}"
SPARK_REPO="${SPARK_REPO:-~/code/inresearch.ai}"
REVIEWER="${REVIEWER:-$USER}"
MAPPINGS="$HERE/scripts/mappings/$COMPANY.tsv"
export PYTHONPATH="$HERE/src"
P=(python3 -m fetchspec.pipeline)
say() { printf '\n== %s\n' "$*"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

cd "$HERE"
say "1/7 targets from $INRESEARCH (must be a clean main checkout)"
export GIT_TERMINAL_PROMPT=0   # never stop at a username/password prompt
git -C "$INRESEARCH" checkout -q main && git -C "$INRESEARCH" pull -q --ff-only || {
  echo "cannot pull $INRESEARCH without a password. Use the ssh remote once:"
  echo "  git -C $INRESEARCH remote set-url origin git@github.com:niuroumiantt/inresearch.ai.git"; exit 1; }
SNAPSHOT=$("${P[@]}" sync-targets --upstream "$INRESEARCH" | json "d['snapshot_id']")
echo "snapshot $SNAPSHOT"

say "2/7 collect reviewed seeds for $COMPANY"
"${P[@]}" collect-seeds --company "$COMPANY" --bind --budget 50 \
  | json "'counts', d['counts'], 'requests', d['requests_used'], 'bound', d['bound_targets']"

say "3/7 map reviewed parameters ($MAPPINGS, reviewer $REVIEWER)"
PRODUCTS=()
while IFS=$'\t' read -r product table row cell field unit condition target; do
  [[ -z "$product" || "$product" == \#* ]] && continue
  [[ "$unit" == "-" ]] && unit=""   # read collapses empty tab fields, so the file writes "-" for no unit
  "${P[@]}" map-field --company "$COMPANY" --product "$product" --table "$table" --row "$row" --cell "$cell" \
    --field "$field" --unit "$unit" --condition "$condition" --reviewer "$REVIEWER" --target "$target" \
    | json "'  $field <-', repr(d.get('original_text'))"
  [[ " ${PRODUCTS[*]-} " == *" $product "* ]] || PRODUCTS+=("$product")
done < "$MAPPINGS"

say "4/7 package"
ARGS=(); for p in "${PRODUCTS[@]}"; do ARGS+=(--product "$p"); done
PKG=$("${P[@]}" package --company "$COMPANY" "${ARGS[@]}" | json "d['package']")
ID=$(basename "$PKG")
ROOT=$(dirname "$(dirname "$PKG")")
OUT="$HOME/fetchspec-deliveries/$ID"; mkdir -p "$OUT"
echo "package $PKG"

say "5/7 independent completeness check"
python3 scripts/verify_package.py "$PKG" --targets "$ROOT/targets/snapshots/$SNAPSHOT/tco_targets.json" \
  --require-format html > "$OUT/verify.json" || { cat "$OUT/verify.json"; echo "verifier found problems; stopping"; exit 1; }
json "'complete', d['complete'], {t: len(e['observations']) for t, e in d['targets'].items()}" < "$OUT/verify.json"
[[ "$MODE" == "--check" ]] && { echo "--check: stopped before transfer. Package: $PKG"; exit 0; }

say "6/7 Spark receive ($SPARK)"
ssh -o BatchMode=yes "$SPARK" true || {
  echo "ssh to $SPARK needs a password. Install this machine's key on Spark once (asks the password one last time):"
  echo "  ssh-copy-id $SPARK"
  echo "or point SPARK at the right host, e.g. SPARK=spark@192.168.50.2"; exit 1; }
if ! ssh "$SPARK" "grep -q '\"json\"' $SPARK_REPO/src/inresearch/materials/fetchspec_receive.py"; then
  if grep -q '"format": "json"' "$PKG/manifest.json"; then
    echo "Spark's receiver does not accept json yet: merge inresearch fetchspec-backflow and git pull on Spark first."; exit 1
  fi
fi
INCOMING=".local/share/inresearch.ai/incoming/$(date +%Y%m%d)-fetchspec/$ID"
ssh "$SPARK" "mkdir -p ~/$INCOMING"
rsync -a "$PKG/" "$SPARK:$INCOMING/"
ssh "$SPARK" "cd $SPARK_REPO && python3 manage.py fetchspec-receive ~/$INCOMING" > "$OUT/receipt.json" \
  || { cat "$OUT/receipt.json"; echo "Spark rejected the package"; exit 1; }
json "'receiver', d['status'], len(d['items']), 'items'" < "$OUT/receipt.json"

say "7/7 production receipt and assignments"
"${P[@]}" receipt --input "$OUT/receipt.json" --environment production | json "d['status']"
"${P[@]}" assignments --delivery-id "$ID" --output "$OUT/assignments.json" --environment production >/dev/null
echo "assignments → $OUT/assignments.json"
echo
echo "Next, in the inresearch author checkout:"
echo "  scripts/author_import.sh $OUT/assignments.json"
