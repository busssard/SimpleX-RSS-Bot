#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Config ---
INSTALL_DIR="/opt/simplex-bot"
ENV_FILE="$INSTALL_DIR/.env"

echo "=== SimpleX RSS Bot Setup ==="

# 1. Download simplex-chat CLI
if ! command -v simplex-chat &>/dev/null; then
    echo "Downloading simplex-chat..."
    curl -fL -o /tmp/simplex-chat \
        https://github.com/simplex-chat/simplex-chat/releases/download/v6.4.11/simplex-chat-ubuntu-24_04-x86_64
    chmod +x /tmp/simplex-chat
    mv /tmp/simplex-chat /usr/local/bin/simplex-chat
fi

# 2. Deploy bot code
echo "Deploying to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$PROJECT_DIR/bot.py" "$INSTALL_DIR/"

# Reset DB if schema changed (bot recreates it on startup)
if [ -f "$INSTALL_DIR/rss.db" ]; then
    OLD_SCHEMA=$(sqlite3 "$INSTALL_DIR/rss.db" ".schema subscriptions" 2>/dev/null || true)
    if ! echo "$OLD_SCHEMA" | grep -q "chat_type"; then
        echo "DB schema changed, resetting rss.db..."
        rm -f "$INSTALL_DIR/rss.db"
    fi
fi

# 3. Configure .env (first run or if missing)
if [ ! -f "$ENV_FILE" ]; then
    echo ""
    read -rp "Set admin password for /botadmin command: " ADMIN_PW
    read -rp "Feed check interval in seconds [300]: " CHECK_INT
    CHECK_INT="${CHECK_INT:-300}"

    cat > "$ENV_FILE" <<EOF
ADMIN_PASSWORD=$ADMIN_PW
CHECK_INTERVAL=$CHECK_INT
EOF
    chmod 600 "$ENV_FILE"
    echo "Config saved to $ENV_FILE"
else
    echo "Config exists at $ENV_FILE (keeping existing)"
fi

# 4. Install Python deps
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q python-simplex-bot feedparser

# 5. Install systemd units and reload
echo "Installing services..."
cp "$SCRIPT_DIR/simplex-chat.service" /etc/systemd/system/
cp "$SCRIPT_DIR/simplex-rss-bot.service" /etc/systemd/system/
systemctl daemon-reload

# 6. Enable and (re)start
systemctl enable simplex-chat simplex-rss-bot
systemctl stop simplex-rss-bot simplex-chat 2>/dev/null || true
systemctl start simplex-chat
sleep 5
systemctl start simplex-rss-bot

echo ""
echo "=== Done ==="
systemctl status simplex-chat simplex-rss-bot --no-pager
echo ""
echo "View logs: journalctl -u simplex-rss-bot -f"
