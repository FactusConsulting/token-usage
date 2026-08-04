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
#   * token-usage entry point in ~/.local/bin
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
BIN_DIR="$HOME/.local/bin"

[ -f "$SHIM_SRC" ] || { echo "[install] missing $SHIM_SRC — set REPO_ROOT env var to the repo path"; exit 1; }

echo "[install] install dir: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$UNIT_DIR" "$BIN_DIR"

# 1. Node 20 — only install if missing or older than 20.
if ! command -v node >/dev/null 2>&1 || \
   [ "$(node --version | sed 's/v\([0-9]*\).*/\1/')" -lt 20 ]; then
    echo "[install] installing Node.js 20 via nodesource..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    echo "[install] node $(node --version) already present"
fi

# 2. ccusage via npm -g, pinned to the version in CCUSAGE_VERSION at repo root.
# Packagers (choco / brew / nix) all read this same file, so a single bump
# propagates everywhere.
CCUSAGE_VERSION_FILE="$REPO_ROOT/CCUSAGE_VERSION"
if [ -f "$CCUSAGE_VERSION_FILE" ]; then
    CCUSAGE_VERSION="$(tr -d '\n' < "$CCUSAGE_VERSION_FILE")"
    echo "[install] (re)installing ccusage@$CCUSAGE_VERSION globally..."
    sudo npm install -g "ccusage@$CCUSAGE_VERSION"
else
    echo "[install] CCUSAGE_VERSION not found, installing latest ccusage..."
    sudo npm install -g ccusage
fi

# Where ccusage actually landed. With nvm-managed node that is somewhere under
# ~/.nvm/versions/node/<v>/bin, which is on neither systemd's PATH nor a
# non-interactive shell's (nvm.sh is sourced from .bashrc, which returns early
# when non-interactive) — the shim then dies with "ccusage not found on PATH".
# Resolve it once here and bake it into both the unit and the wrapper.
# `command -v ccusage` first; otherwise ask the npm we just installed with,
# since ccusage lands in that npm's global bin dir.
CCUSAGE_BIN_DIR=""
for candidate in \
    "$(command -v ccusage 2>/dev/null || true)" \
    "$(dirname "$(command -v npm 2>/dev/null || echo /nonexistent)")/ccusage" \
    "$(npm prefix -g 2>/dev/null || echo /nonexistent)/bin/ccusage"
do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        CCUSAGE_BIN_DIR="$(cd "$(dirname "$candidate")" && pwd)"
        echo "[install] ccusage resolved to $candidate"
        break
    fi
done
BASE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [ -n "$CCUSAGE_BIN_DIR" ]; then
    RUN_PATH="$CCUSAGE_BIN_DIR:$BASE_PATH"
else
    RUN_PATH="$BASE_PATH"
    echo "[install] WARNING: could not locate the ccusage binary — runs will fail" \
         "until it is on PATH. Check 'npm ls -g ccusage'."
fi

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

# 5. `token-usage` entry point. The Homebrew and Nix channels expose one and the
# docs tell every user to run `token-usage --dry-run`; without this the command
# doesn't exist on a clone install. The systemd unit keeps calling the venv
# python directly — it must not depend on $HOME/.local/bin being on PATH.
# An empty CCUSAGE_BIN_DIR must not become a bare ":" in PATH — that is an empty
# entry, which POSIX reads as the current directory.
if [ -n "$CCUSAGE_BIN_DIR" ]; then
    PATH_LINE="export PATH=\"$CCUSAGE_BIN_DIR:\$PATH\""
else
    PATH_LINE="# (ccusage was not found at install time; relying on the caller's PATH)"
fi

cat > "$BIN_DIR/token-usage" <<EOF
#!/usr/bin/env bash
# token-usage CLI entry point. Interactive counterpart to ccusage-ship.timer.
# Prepends the ccusage resolved at install time, so this works from a plain
# non-login shell too (mirrors what the Homebrew wrapper does).
set -euo pipefail
$PATH_LINE
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/ccusage-ship.py" "\$@"
EOF
chmod 0755 "$BIN_DIR/token-usage"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "[install] NOTE: $BIN_DIR is not on PATH in this shell — add it to use 'token-usage'" ;;
esac

# 6. systemd user units.
cat > "$UNIT_DIR/ccusage-ship.service" <<EOF
[Unit]
Description=Ship ccusage daily aggregates to Langfuse
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# systemd's default PATH does not include an nvm node, so spell out where
# ccusage was found at install time.
Environment=PATH=$RUN_PATH
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
echo "         token-usage --dry-run                 # print the batch, don't send"
echo "         systemctl --user start ccusage-ship.service && tail $INSTALL_DIR/ship.log"
