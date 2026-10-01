#!/usr/bin/env bash
# Bring Spark's inresearch.ai checkout to GitHub main, from a machine that can pull GitHub (M5).
#
#   scripts/update_spark.sh
#
# Spark keeps no GitHub token (inresearch docs/local_reader/SPARK_OPERATIONS.md, "后续源码更新"):
# the authenticated machine sends main's objects over ssh, Spark then fast-forwards its own main.
# Follows that procedure: refuse a dirty checkout, stop the reader while the source changes,
# merge --ff-only to the verified SHA, record it as READER_RELEASE, start the reader again only
# if it was running. Never resets, stashes or rsyncs over the source.
#
# Settings: INRESEARCH (default ~/code/inresearch.ai), SPARK (default spark), SPARK_REPO (default code/inresearch.ai,
# relative to the Spark home).
set -euo pipefail
INRESEARCH="${INRESEARCH:-$HOME/code/inresearch.ai}"
SPARK="${SPARK:-spark}"
SPARK_REPO="${SPARK_REPO:-code/inresearch.ai}"
export GIT_TERMINAL_PROMPT=0

git -C "$INRESEARCH" fetch -q origin main
SHA=$(git -C "$INRESEARCH" rev-parse origin/main)
echo "GitHub main $SHA"

DIRTY=$(ssh "$SPARK" "cd $SPARK_REPO && git status --porcelain")
[[ -z "$DIRTY" ]] || { echo "Spark checkout has local changes; not touching it:"; echo "$DIRTY"; exit 1; }

# Objects only: updates Spark's remote-tracking ref, never its working tree or branch.
git -C "$INRESEARCH" push -q "$SPARK:$SPARK_REPO" "$SHA:refs/remotes/origin/main"

ssh "$SPARK" "SHA=$SHA REPO=$SPARK_REPO bash -s" <<'EOF'
set -euo pipefail
cd "$HOME/$REPO"
git checkout -q main
git merge-base --is-ancestor HEAD "$SHA" || { echo "Spark main is not an ancestor of $SHA; stopping"; exit 1; }
was=$(systemctl --user is-active inresearch-reader.service 2>/dev/null || true)
[[ "$was" == active ]] && systemctl --user stop inresearch-reader.service
git merge -q --ff-only "$SHA"
env_file="$HOME/.config/inresearch.ai/reader.env"
if [[ -f "$env_file" ]] && grep -q '^READER_RELEASE=' "$env_file"; then
  sed -i "s/^READER_RELEASE=.*/READER_RELEASE=$SHA/" "$env_file"
fi
[[ "$was" == active ]] && systemctl --user start inresearch-reader.service
echo "Spark main $(git log --oneline -1)"
echo "reader: ${was:-inactive} -> $(systemctl --user is-active inresearch-reader.service 2>/dev/null || true)"
grep -q '"json"' src/inresearch/materials/fetchspec_receive.py && echo "receiver accepts json"
EOF
