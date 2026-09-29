#!/usr/bin/env bash
# Install the hourly auto-update for the ttv service. Run as root:
#   cd /home/TwitchDropsMiner && bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")"
install -m 755 ttv-update.sh /usr/local/bin/ttv-update
install -m 644 ttv-update.service ttv-update.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ttv-update.timer

echo "== restarting ttv on the current code"
systemctl restart ttv
sleep 15
systemctl status ttv --no-pager -n 10 || true

echo
echo "== auto-update installed; next check:"
systemctl list-timers ttv-update.timer --no-pager
echo "Logs: journalctl -u ttv-update"
