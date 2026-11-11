#!/bin/bash
# Run `archive run` unattended (for launchd or cron) and send a macOS notification
# when new posts arrive or something needs attention. Output goes to the run log
# that `archive` writes in <output_dir>/logs/. One run covers every newsletter in
# config.yaml; a newsletter whose login expired is named in the notification.
set -u
cd "$(dirname "$0")/.." || exit 1
# launchd starts jobs with a minimal PATH; Docker Desktop installs its CLI here.
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

# The text goes to AppleScript as an argument, never inside the script, so a quote or
# backslash in a newsletter name or an error can't break the notification or run anything.
notify() {
  command -v osascript >/dev/null 2>&1 &&
    osascript -e 'on run argv' \
      -e 'display notification (item 1 of argv) with title "Newsletter archive"' \
      -e 'end run' "$1" >/dev/null 2>&1
  return 0
}

# The search index lives in the Docker database; start it (no-op if it's running). If it can't
# start, `archive run` still archives new posts and exits 3, which notifies below.
# ARCHIVE_SKIP_DB_START=1 skips this (the script's tests use it, so they never touch Docker).
if [ -z "${ARCHIVE_SKIP_DB_START:-}" ]; then
  command -v docker >/dev/null 2>&1 && docker compose up -d --wait db >/dev/null 2>&1
fi

output=$(.venv/bin/archive run --config config.yaml 2>&1)
code=$?
echo "$output" | tail -3

# New posts, totalled across newsletters.
fetched=$(echo "$output" | sed -n 's/^Summary: .*fetched=\([0-9]*\).*/\1/p' | tail -1)
# One line per newsletter that stopped, "Stopped: <name> (<ENV>): <reason>",
# shown as "<name>: <reason> (<ENV>)." (e.g. "The Pragmatic Engineer: your cookie looks
# expired. Paste a fresh Cookie header into .env (PRAGMATIC_ENGINEER_COOKIE).")
stopped=$(echo "$output" | sed -n 's/^Stopped: \(.*\) (\([A-Z][A-Z0-9_]*\)): \(.*\)$/\1: \3 (\2)./p')
# Newsletters in the run: those with stage lines ("== <name>: <stage>") or a Stopped line.
sources=$(echo "$output" | sed -n -e 's/^== \(.*\): [a-z]*.*$/\1/p' -e 's/^Stopped: \(.*\) ([A-Z][A-Z0-9_]*): .*$/\1/p' | sort -u | wc -l | tr -d ' ')

# "<name>: <reason> (<ENV>)." for each stopped newsletter, plus "Others updated." when others ran.
stopped_message=""
if [ -n "$stopped" ]; then
  stopped_message=$(echo "$stopped" | paste -sd ' ' -)
  if [ "$(echo "$stopped" | wc -l | tr -d ' ')" -lt "${sources:-0}" ]; then
    stopped_message="$stopped_message Others updated."
  fi
fi

case $code in
  0) [ "${fetched:-0}" -gt 0 ] && notify "Archived ${fetched} new post(s)." ;;
  1|2)
    if [ -n "$stopped_message" ]; then
      notify "$stopped_message"
    elif [ "$code" -eq 2 ]; then
      notify "Your cookie looks expired. Paste a fresh Cookie header into .env."
    else
      notify "Update failed (exit ${code}). See the latest log in the archive's logs folder."
    fi ;;
  3)
    # An index failure outranks a stopped newsletter in the exit code; name both.
    index_message="New posts were archived, but updating the search index failed. Is Docker running? See the latest log."
    notify "${stopped_message:+$stopped_message }$index_message" ;;
  *) notify "Update failed (exit ${code}). See the latest log in the archive's logs folder." ;;
esac
exit $code
