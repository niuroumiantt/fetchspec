#!/usr/bin/env bash
# Author step: import production assignments.json files into inresearch on one branch and push it for review.
#
#   scripts/author_import.sh ~/fetchspec-deliveries/<delivery-id>/assignments.json [more assignments.json ...]
#
# Several deliveries go on one branch, so the site owner reviews and signs the target table once.
# INRESEARCH (default ~/code/inresearch.ai) must be a clean checkout you can push from.
# The PR still needs the site owner's review signature before merge; this script never merges.
set -euo pipefail
[[ $# -ge 1 ]] || { echo "usage: author_import.sh <assignments.json> [more ...]"; exit 2; }
INRESEARCH="${INRESEARCH:-$HOME/code/inresearch.ai}"
HERE="$(cd "$(dirname "$0")" && pwd)"
FILES=(); IDS=()
for f in "$@"; do
  path="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
  FILES+=("$path")
  IDS+=("$(python3 -c "import json,sys; r=json.load(open(sys.argv[1]))['records']; print(r[0]['fetchspec']['delivery_id'])" "$path")")
done
if [[ ${#IDS[@]} -eq 1 ]]; then
  BRANCH="fetchspec-delivery-${IDS[0]#fetchspec-}"
else
  BRANCH="fetchspec-deliveries-$(date +%Y%m%d-%H%M%S)"
fi

cd "$INRESEARCH"
[[ -z "$(git status --porcelain)" ]] || { echo "$INRESEARCH has local changes; commit or stash them first"; exit 1; }
export GIT_TERMINAL_PROMPT=0
git checkout -q main && git pull -q --ff-only || { echo "pull needs a password; run: git remote set-url origin git@github.com:niuroumiantt/inresearch.ai.git"; exit 1; }
git checkout -q -B "$BRANCH"   # re-importing the same delivery (e.g. to add values) starts again from main
for path in "${FILES[@]}"; do
  python3 manage.py deliveries import --assignments "$path" --by "${USER}"
done
python3 manage.py deliveries check
python3 manage.py dashboard --refresh >/dev/null   # generated from the target table; CI fails if stale
# the generated registry; refused (and left to sign_review.sh) while a reviewed file awaits its signature
python3 manage.py governance --refresh >/dev/null 2>&1 || true
git add data/event_cards.json framework/tco_targets.json data/dashboard.json framework/repository_manifest.json docs/REPOSITORY_REGISTER.md
git commit -q -m "deliveries: import Fetchspec ${IDS[*]} (production receipt)"
git push -q -u origin "$BRANCH"
echo "Pushed $BRANCH (${#IDS[@]} deliveries). Open the PR: https://github.com/niuroumiantt/InResearch.ai/pull/new/$BRANCH"
CHECK=$(python3 manage.py governance --check 2>&1 || true)   # exits 1 while a review is pending
if grep -q 'review required' <<<"$CHECK"; then
  echo
  echo "CI needs the site owner's review of reviewed files before merge. As the reviewer, run:"
  echo "  $HERE/sign_review.sh"
fi
