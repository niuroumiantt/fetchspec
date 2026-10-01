#!/usr/bin/env bash
# Site owner's sign-off on the current inresearch branch: review, then record it.
#
#   cd ~/code/inresearch.ai && git checkout <branch> && ~/code/fetchspec/scripts/sign_review.sh
#
# Shows the diff of every reviewed file `governance --check` says changed, asks the reviewer to
# confirm, then records the new digests in framework/verification_contract.json, refreshes the
# generated registry, runs the CI checks (stopping on any failure), commits and pushes the branch.
# Nothing is recorded without an explicit "y". Never merges.
set -euo pipefail
INRESEARCH="${INRESEARCH:-$HOME/code/inresearch.ai}"
cd "$INRESEARCH"
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[[ "$BRANCH" != main ]] || { echo "check out the branch to sign, not main"; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "$INRESEARCH has local changes; commit or stash them first"; exit 1; }
export GIT_TERMINAL_PROMPT=0
git fetch -q origin main
FILES=$(python3 manage.py governance --check 2>&1 | sed -n 's/.*review required: //p' | sort -u || true)
[[ -n "$FILES" ]] || { echo "no reviewed file needs a signature on $BRANCH"; exit 0; }
echo "Reviewed files changed on $BRANCH:"; echo "$FILES" | sed 's/^/  /'
# shellcheck disable=SC2086
git --no-pager diff origin/main...HEAD -- $FILES
# Drop lines pasted after this command, so only a key typed now can answer.
while read -r -t 1 _ </dev/tty 2>/dev/null; do :; done
read -r -p "Signed off by ${USER:-$(id -un)} after reading the diff above? [y/N] " ok </dev/tty
[[ "$ok" == y || "$ok" == Y ]] || { echo "not signed"; exit 1; }
python3 - $FILES <<'PY'
import hashlib, re, sys
p = 'framework/verification_contract.json'
text = open(p, encoding='utf-8').read()
for f in sys.argv[1:]:
    digest = hashlib.sha256(open(f, 'rb').read()).hexdigest()
    text, n = re.subn(r'("%s": ")[0-9a-f]{64}"' % re.escape(f), r'\g<1>%s"' % digest, text)
    assert n == 1, f
open(p, 'w', encoding='utf-8').write(text)
PY
python3 manage.py governance --refresh >/dev/null
python3 manage.py governance --check
python3 manage.py validate --strict | tail -1
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -q 2>&1 | tail -3
git add framework/verification_contract.json framework/repository_manifest.json docs/REPOSITORY_REGISTER.md
git commit -q -m "评审：$(echo $FILES | tr ' ' '、') 摘要（$BRANCH）"
git push -q
echo "Signed and pushed $BRANCH. Merge its PR after CI is green."
