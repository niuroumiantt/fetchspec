#!/usr/bin/env bash
# Deliver several companies to production in one go, then import them all on one inresearch branch.
#
#   scripts/deliver_all.sh vertiv nvidia supermicro asteralabs siemens siemens-energy delta
#   EXTRA="~/fetchspec-deliveries/<id>/assignments.json" scripts/deliver_all.sh ...   # also import an earlier delivery
#
# Runs scripts/deliver_production.sh for each company (collect → map → package → verify → Spark receive →
# receipt → assignments). A company that fails is reported and skipped; the others continue. Then every
# assignments.json produced (plus EXTRA) goes to scripts/author_import.sh on a single branch, so the site
# owner signs the target table once. Logs: ~/fetchspec-deliveries/runs/<time>/<company>.log
set -uo pipefail
[[ $# -ge 1 ]] || { echo "usage: deliver_all.sh <company> [company ...]"; exit 2; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RUN="$HOME/fetchspec-deliveries/runs/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$RUN"
FILES=(); OK=(); FAILED=()
for f in ${EXTRA:-}; do f="${f/#\~/$HOME}"; [[ -f "$f" ]] && FILES+=("$f") || { echo "EXTRA file not found: $f"; exit 1; }; done

for company in "$@"; do
  printf '\n######## %s\n' "$company"
  bash "$HERE/scripts/deliver_production.sh" "$company" 2>&1 | tee "$RUN/$company.log"
  status=${PIPESTATUS[0]}
  path=$(sed -n 's/^assignments → //p' "$RUN/$company.log" | tail -1)
  if [[ $status -eq 0 && -f "$path" ]]; then
    FILES+=("$path"); OK+=("$company")
  else
    FAILED+=("$company")
  fi
done

printf '\n== delivered: %s\n' "${OK[*]:-none}"
[[ ${#FAILED[@]} -eq 0 ]] || printf '== failed (see %s/<company>.log): %s\n' "$RUN" "${FAILED[*]}"
[[ ${#FILES[@]} -gt 0 ]] || { echo "nothing to import"; exit 1; }
printf '\n== author import of %d assignments file(s)\n' "${#FILES[@]}"
bash "$HERE/scripts/author_import.sh" "${FILES[@]}"
