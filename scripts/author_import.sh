#!/usr/bin/env bash
# Author step: import a production assignments.json into inresearch on a new branch and push it for review.
#
#   scripts/author_import.sh ~/fetchspec-deliveries/<delivery-id>/assignments.json
#
# INRESEARCH (default ~/code/inresearch.ai) must be a clean checkout you can push from.
# The PR still needs the site owner's review signature before merge; this script never merges.
set -euo pipefail
ASSIGNMENTS="$(cd "$(dirname "${1:?usage: author_import.sh <assignments.json>}")" && pwd)/$(basename "$1")"
INRESEARCH="${INRESEARCH:-$HOME/code/inresearch.ai}"
ID=$(python3 -c "import json,sys; r=json.load(open(sys.argv[1]))['records']; print(r[0]['fetchspec']['delivery_id'])" "$ASSIGNMENTS")
BRANCH="fetchspec-delivery-${ID#fetchspec-}"

cd "$INRESEARCH"
[[ -z "$(git status --porcelain)" ]] || { echo "$INRESEARCH has local changes; commit or stash them first"; exit 1; }
export GIT_TERMINAL_PROMPT=0
git checkout -q main && git pull -q --ff-only || { echo "pull needs a password; run: git remote set-url origin git@github.com:niuroumiantt/inresearch.ai.git"; exit 1; }
git checkout -q -b "$BRANCH"
python3 manage.py deliveries import --assignments "$ASSIGNMENTS" --by "${USER}"
python3 manage.py deliveries check
git add data/event_cards.json framework/tco_targets.json
git commit -q -m "deliveries: import Fetchspec $ID (production receipt)"
git push -q -u origin "$BRANCH"
echo "Pushed $BRANCH. Open the PR: https://github.com/niuroumiantt/InResearch.ai/pull/new/$BRANCH"
