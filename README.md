# SimpleX RSS Bot

An RSS/Atom feed bot for [SimpleX Chat](https://simplex.chat). Subscribe to feeds in direct messages or groups, and get new posts delivered automatically.

Works with both **direct chats** and **groups** -- add the bot to a group, subscribe to feeds there, and everyone in the group gets the updates.

## Features

- `/sub <url> [--newest]` -- Subscribe to a feed. `--newest` sends the latest post immediately.
- `/unsub <url>` -- Unsubscribe from a feed.
- `/list` -- Show active subscriptions for this chat.
- `/help` -- Show available commands.
- `/botadmin <password>` -- Show admin stats (user count, group count, feed count).
- Auto-accepts contact requests and group invitations.
- Checks feeds every 5 minutes (configurable).
- Exponential backoff on failing feeds, deactivates after 100 consecutive errors.
- HTML-to-plaintext conversion for feed content.
- Media URL extraction from HTML and RSS enclosures.

## Requirements

- Linux server (tested on Ubuntu 24.04)
- Python 3.12+
- `simplex-chat` CLI binary

## Quick Setup

```bash
git clone https://github.com/youruser/simpleXbot.git
cd simpleXbot
bash deploy/setup.sh
```

The setup script will:

1. Download the `simplex-chat` CLI if not installed
2. Deploy `bot.py` to `/opt/simplex-bot/`
3. Ask for an admin password and check interval on first run (saved to `/opt/simplex-bot/.env`)
4. Create a Python venv and install dependencies
5. Install and start systemd services

On first run, `simplex-chat` creates a bot identity. The bot address is printed in the logs:

```bash
journalctl -u simplex-rss-bot -f
```

Share that address with users so they can connect via the SimpleX app.

## Configuration

Config lives in `/opt/simplex-bot/.env` (created by the setup script):

```
ADMIN_PASSWORD=yourpassword
CHECK_INTERVAL=300
```

To change settings, edit the file and restart:

```bash
nano /opt/simplex-bot/.env
systemctl restart simplex-rss-bot
```

## Manual Setup

If you don't want to use the setup script:

```bash
# Install simplex-chat
curl -fL -o /usr/local/bin/simplex-chat \
    https://github.com/simplex-chat/simplex-chat/releases/download/v6.4.11/simplex-chat-ubuntu-24_04-x86_64
chmod +x /usr/local/bin/simplex-chat

# Start simplex-chat with websocket
simplex-chat -p 5225

# In another terminal
python3 -m venv .venv
.venv/bin/pip install python-simplex-bot feedparser
cp .env.example .env  # edit with your settings
.venv/bin/python bot.py
```

## Useful Commands

```bash
# Check if services are running
systemctl status simplex-chat simplex-rss-bot

# View bot logs
journalctl -u simplex-rss-bot -f

# View simplex-chat logs
journalctl -u simplex-chat -f

# Restart after code changes
systemctl restart simplex-rss-bot

# Inspect the database
sqlite3 /opt/simplex-bot/rss.db "SELECT * FROM subscriptions;"
```

## Working with python-simplex-bot

This bot uses [python-simplex-bot](https://pypi.org/project/python-simplex-bot/) (v0.0.1), but overrides most of its internals because the library's Pydantic models are incompatible with simplex-chat v6.4.x. Specifically:

- **Response format mismatch**: The library expects `resp.Right.type` / `resp.Left.type` wrappers, but simplex-chat v6.4.x sends `resp.type` directly. This breaks all Pydantic parsing.
- **`_connect` override**: The library's connect method crashes with `KeyError: 'Left'` when the bot address already exists. We override it to handle the response format robustly.
- **`_on_message` override**: We parse all incoming websocket messages as raw JSON instead of using the library's broken Pydantic models. This handles contact requests, group invitations, and text messages directly.
- **`send_to` method**: The library's `send_text` only supports direct messages. We added `send_to()` which uses `#groupId` for groups and `@contactId` for direct chats.
- **No decorator handlers**: The library's `@bot.command_handler` / `@bot.text_handler` decorators depend on the broken parser. All command routing is done manually in `handle_command()`.

If `python-simplex-bot` updates to support the v6.4.x response format, these overrides can be removed.

## License

MIT
