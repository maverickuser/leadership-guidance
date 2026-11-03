#!/bin/bash
# Run `archive run` unattended (for launchd or cron) and send a macOS notification
# when new posts arrive or something needs attention. Output goes to the run log
# that `archive` writes in <output_dir>/logs/.
set -u
cd "$(dirname "$0")/.." || exit 1
# launchd starts jobs with a minimal PATH; Docker Desktop installs its CLI here.
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

notify() {
  command -v osascript >/dev/null 2>&1 &&
    osascript -e "display notification \"$1\" with title \"Newsletter archive\"" >/dev/null 2>&1
  return 0
}

# The search index lives in the Docker database; start it (no-op if it's running). If it can't
# start, `archive run` still archives new posts and exits 3, which notifies below.
command -v docker >/dev/null 2>&1 && docker compose up -d --wait db >/dev/null 2>&1

output=$(.venv/bin/archive run --config config.yaml 2>&1)
code=$?
echo "$output" | tail -3

fetched=$(echo "$output" | sed -n 's/^Summary: .*fetched=\([0-9]*\).*/\1/p' | tail -1)
case $code in
  0) [ "${fetched:-0}" -gt 0 ] && notify "Archived ${fetched} new post(s)." ;;
  2) notify "Your cookie looks expired. Paste a fresh Cookie header into .env." ;;
  3) notify "New posts were archived, but updating the search index failed. Is Docker running? See the latest log." ;;
  *) notify "Update failed (exit ${code}). See the latest log in the archive's logs folder." ;;
esac
exit $code
