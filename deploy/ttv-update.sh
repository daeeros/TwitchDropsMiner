#!/usr/bin/env bash
# Pull new commits of TwitchDropsMiner from GitHub and restart the ttv service.
# Safe by design:
#   - fast-forward only: never merges, never overwrites local edits
#   - compiles every .py file before restarting
#   - rolls back to the previous commit if the check fails or the service
#     doesn't stay up after the restart
# Logs go to journald:  journalctl -u ttv-update
set -euo pipefail

APP_DIR="${APP_DIR:-/home/TwitchDropsMiner}"
BRANCH="${BRANCH:-master}"
SERVICE="${SERVICE:-ttv}"
PY="$APP_DIR/venv/bin/python"
SETTLE_SECONDS="${SETTLE_SECONDS:-60}"
STATE_DIR="${STATE_DIR:-/var/lib/ttv-update}"

git_() { git -C "$APP_DIR" -c safe.directory="$APP_DIR" "$@"; }

cd "$APP_DIR"

git_ fetch --quiet origin "$BRANCH"
OLD=$(git_ rev-parse HEAD)
NEW=$(git_ rev-parse "origin/$BRANCH")

if [[ "$OLD" == "$NEW" ]]; then
    echo "Up to date at ${OLD:0:7}."
    exit 0
fi

# Don't retry (and restart the miner) every hour for a commit that already failed.
mkdir -p "$STATE_DIR"
if [[ "$(cat "$STATE_DIR/bad-commit" 2>/dev/null || true)" == "$NEW" ]]; then
    echo "${NEW:0:7} failed before - waiting for a newer commit."
    exit 0
fi

# Tracked files edited by hand on the server: don't touch them.
# (settings.json, cookies and the cache are untracked, so they are never affected.)
if [[ -n "$(git_ status --porcelain --untracked-files=no)" ]]; then
    echo "Local changes to tracked files - skipping update. Run 'git status' in $APP_DIR." >&2
    exit 1
fi

if ! git_ merge --quiet --ff-only "origin/$BRANCH"; then
    echo "Server copy has commits that aren't on GitHub - can't fast-forward, skipping." >&2
    exit 1
fi
echo "Updated ${OLD:0:7} -> ${NEW:0:7}:"
git_ log --oneline "$OLD..$NEW"

rollback() {
    echo "ROLLBACK: $1 - returning to ${OLD:0:7}." >&2
    echo "$NEW" > "$STATE_DIR/bad-commit"
    git_ reset --quiet --hard "$OLD"
    if ! git_ diff --quiet "$OLD" "$NEW" -- requirements.txt; then
        "$PY" -m pip install --quiet -r requirements.txt || true
    fi
    systemctl restart "$SERVICE"
    exit 1
}

if ! git_ diff --quiet "$OLD" "$NEW" -- requirements.txt; then
    echo "requirements.txt changed - installing."
    "$PY" -m pip install --quiet -r requirements.txt || rollback "pip install failed"
fi

# Syntax-check every tracked Python file before touching the running service.
git_ ls-files -z '*.py' | xargs -0 -r "$PY" -m py_compile || rollback "compile check failed"

restart_count() { systemctl show -p NRestarts --value "$SERVICE"; }
BEFORE=$(restart_count)
systemctl restart "$SERVICE"
sleep "$SETTLE_SECONDS"

# The unit has Restart=always, so a crashing build shows up as automatic
# restarts rather than as "failed".
STATE=$(systemctl is-active "$SERVICE" || true)
AFTER=$(restart_count)
# (if this systemd resets the counter on a manual restart, AFTER is the count itself)
RESTARTS=$(( AFTER >= BEFORE ? AFTER - BEFORE : AFTER ))
if [[ "$STATE" != "active" || "$RESTARTS" -gt 0 ]]; then
    rollback "service is '$STATE' with $RESTARTS restart(s) after ${SETTLE_SECONDS}s"
fi

echo "Restarted $SERVICE on ${NEW:0:7}; running fine after ${SETTLE_SECONDS}s."
