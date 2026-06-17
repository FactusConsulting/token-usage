#!/usr/bin/env bash
# Install ccusage + the token-usage shim on Ubuntu and register a systemd
# user timer that runs hourly.
#
# Idempotent. Re-running reconciles:
#   * Node 20 (nodesource APT repo — skipped if already at >= 20)
#   * ccusage (npm -g, upgraded to latest)
#   * shim copied to ~/.local/share/token-usage/
#   * venv at ~/.local/share/token-usage/.venv with deps
#   * .env created with placeholders if missing (user fills it in)
#   * systemd user units installed and timer enabled
#
# After install, edit ~/.config/token-usage/.env then `systemctl --user start
# ccusage-ship.service` to test, or wait for the hourly timer.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
SHIM_SRC="$REPO_ROOT/shim/ccusage-ship.py"
ENV_EXAMPLE="$REPO_ROOT/shim/.env.example"
REQ_FILE="$REPO_ROOT/shim/requirements.txt"

INSTALL_DIR="$HOME/.local/share/token-usage"
CONFIG_DIR="$HOME/.config/token-usage"
UNIT_DIR="$HOME/.config/systemd/user"

[ -f "$SHIM_SRC" ] || { echo "[install] missing $SHIM_SRC — set REPO_ROOT env var to the repo path"; exit 1; }

echo "[install] install dir: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$UNIT_DIR"

# 1. Node 20 — only install if missing or older than 20.
if ! command -v node >/dev/null 2>&1 || \
   [ "$(node --version | sed 's/v\([0-9]*\).*/\1/')" -lt 20 ]; then
    echo "[install] installing Node.js 20 via nodesource..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    echo "[install] node $(node --version) already present"
fi

# 2. ccusage via npm -g.
echo "[install] (re)installing ccusage globally..."
sudo npm install -g ccusage

# 3. Shim file + env template.
install -m 0644 "$SHIM_SRC" "$INSTALL_DIR/ccusage-ship.py"
if [ ! -f "$CONFIG_DIR/.env" ]; then
    install -m 0600 "$ENV_EXAMPLE" "$CONFIG_DIR/.env"
    echo "[install] WROTE placeholder $CONFIG_DIR/.env — edit it to set LANGFUSE_* keys"
else
    echo "[install] existing $CONFIG_DIR/.env preserved"
fi

# 4. Python venv + deps.
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$REQ_FILE"

# 5. systemd user units.
cat > "$UNIT_DIR/ccusage-ship.service" <<EOF
[Unit]
Description=Ship ccusage daily aggregates to Langfuse
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$CONFIG_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/ccusage-ship.py
StandardOutput=append:$INSTALL_DIR/ship.log
StandardError=append:$INSTALL_DIR/ship.log
EOF

cat > "$UNIT_DIR/ccusage-ship.timer" <<EOF
[Unit]
Description=Run ccusage-ship hourly

[Timer]
# Five minutes after every full hour, so all PCs do not hit Langfuse at :00.
OnCalendar=*-*-* *:05:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ccusage-ship.timer

# Linger lets the user timer fire even when the user is not logged in.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    echo "[install] enabling linger for $USER so the timer fires when logged out..."
    sudo loginctl enable-linger "$USER"
fi

echo "[install] Done. Edit $CONFIG_DIR/.env, then test with:"
echo "         systemctl --user start ccusage-ship.service && tail $INSTALL_DIR/ship.log"
