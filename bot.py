import asyncio
import json
import os
import sqlite3
import hashlib
import time
import re
from pathlib import Path
from html.parser import HTMLParser
from dataclasses import dataclass

import websockets
import feedparser
from python_simplex_bot import Bot
from python_simplex_bot.types import BaseContext, UpdateTextMessage, UpdateNewContact, Peer, User
from python_simplex_bot.websocket_client.commands import (
    CmdCreateAddressIfNotExists, CmdShowAddress, CmdSendMessage, ComposedMessage
)
from python_simplex_bot.websocket_client.datatypes import MCText

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DB_PATH = Path(__file__).parent / "rss.db"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_ERRORS = 100      # deactivate feed after this many consecutive errors
BACKOFF_CAP = 86400   # max backoff: 1 day


# --- HTML to Plaintext ---

class _HTMLToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._link_href: str | None = None
        self._skip = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag in ("script", "style"):
            self._skip = True
        elif tag == "br":
            self._parts.append("\n")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
            self._parts.append("\n\n")
        elif tag == "li":
            self._parts.append("\n- ")
        elif tag == "blockquote":
            self._parts.append("\n> ")
        elif tag == "a" and "href" in attrs_d:
            self._link_href = attrs_d["href"]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        elif tag == "a" and self._link_href:
            self._parts.append(f" ({self._link_href})")
            self._link_href = None
        elif tag in ("p", "div", "blockquote"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def handle_entityref(self, name):
        from html import unescape
        self._parts.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        from html import unescape
        self._parts.append(unescape(f"&#{name};"))

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _HTMLToText()
    parser.feed(html)
    return parser.get_text()


# --- Media Extraction ---

@dataclass
class Media:
    url: str
    type: str  # "image", "video", "audio"
    caption: str = ""


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
VIDEO_EXTS = (".mp4", ".webm", ".mov")
AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac")


class _MediaExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.media: list[Media] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        src = attrs_d.get("src", "")
        if tag == "img" and src:
            self.media.append(Media(url=src, type="image", caption=attrs_d.get("alt", "")))
        elif tag == "video" and src:
            self.media.append(Media(url=src, type="video"))
        elif tag == "audio" and src:
            self.media.append(Media(url=src, type="audio"))
        elif tag == "source" and src:
            mime = attrs_d.get("type", "")
            if "video" in mime:
                self.media.append(Media(url=src, type="video"))
            elif "audio" in mime:
                self.media.append(Media(url=src, type="audio"))


def extract_media(html: str, enclosures: list | None = None) -> list[Media]:
    media: list[Media] = []

    if html:
        ext = _MediaExtractor()
        ext.feed(html)
        media.extend(ext.media)

    for enc in enclosures or []:
        url = enc.get("href") or enc.get("url", "")
        mime = enc.get("type", "")
        if not url:
            continue
        low = url.lower().split("?")[0]
        if mime.startswith("image") or low.endswith(IMAGE_EXTS):
            media.append(Media(url=url, type="image"))
        elif mime.startswith("video") or low.endswith(VIDEO_EXTS):
            media.append(Media(url=url, type="video"))
        elif mime.startswith("audio") or low.endswith(AUDIO_EXTS):
            media.append(Media(url=url, type="audio"))

    seen: set[str] = set()
    unique: list[Media] = []
    for m in media:
        if m.url not in seen:
            seen.add(m.url)
            unique.append(m)
    return unique


# --- Entry Content Helper ---

def get_entry_content(entry) -> str:
    if "content" in entry:
        for c in entry.content:
            if c.get("type", "") in ("text/html", "html"):
                return c.value
        return entry.content[0].value
    return entry.get("summary", "")


# --- Database ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY,
            title TEXT,
            last_check REAL DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            next_check REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER,
            chat_type TEXT DEFAULT 'direct',
            feed_url TEXT,
            added_at REAL,
            PRIMARY KEY (chat_id, chat_type, feed_url),
            FOREIGN KEY (feed_url) REFERENCES feeds(url)
        );
        CREATE TABLE IF NOT EXISTS seen_entries (
            feed_url TEXT,
            entry_hash TEXT,
            PRIMARY KEY (feed_url, entry_hash)
        );
    """)
    return conn


db = init_db()


def entry_hash(entry):
    key = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.md5(key.encode()).hexdigest()


# --- Chat target: unified direct + group ---

@dataclass
class ChatTarget:
    chat_id: int
    chat_type: str  # "direct" or "group"
    name: str = ""


# --- Bot ---

class RSSBot(Bot):

    async def start_async(self):
        import signal
        print("Starting bot...")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self.shutdown(s, loop))
            )
        try:
            await self._connect()
        except asyncio.CancelledError:
            print("Bot stopped.")

    async def _connect(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.url) as ws:
                    self.ws = ws
                    handler = asyncio.create_task(self._message_handler())
                    await asyncio.sleep(0.1)

                    address = await self.cmd(CmdCreateAddressIfNotExists())
                    resp = address.get("resp", {})
                    if resp.get("Left") or "Left" not in resp:
                        address = await self.cmd(CmdShowAddress())
                        resp = address.get("resp", {})

                    try:
                        right = resp.get("Right", resp)
                        link = right["contactLink"]["connLinkContact"]["connFullLink"]
                        print(f"Bot started. Address:\n{link}\n")
                    except (KeyError, TypeError):
                        print("Bot started. (Could not extract address from response)")

                    await handler
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Connection error: {e}")
                if self._running:
                    print("Reconnecting in 5s...")
                    await asyncio.sleep(5)

    async def send_to(self, message: str, target: ChatTarget):
        """Send a text message to a direct contact or group."""
        cmd = CmdSendMessage(
            chatType="group" if target.chat_type == "group" else "chat",
            chatId=str(target.chat_id),
            messages=[ComposedMessage(
                filePath=None,
                quotedItemId=None,
                msgContent=MCText(type="text", text=message)
            )]
        )
        await self.cmd(cmd, wait_for_response=False)

    async def _on_message(self, raw_message: str):
        try:
            data = json.loads(raw_message)
            resp = data.get("resp", {})
            msg_type = resp.get("type", "")

            # Auto-accept contact requests
            if msg_type == "receivedContactRequest":
                req_id = resp.get("contactRequest", {}).get("contactRequestId")
                if req_id is not None:
                    await self.cmd(f"/_accept {req_id}", wait_for_response=False)
                    print(f"Auto-accepted contact: {resp.get('contactRequest', {}).get('localDisplayName', '?')}")
                return

            # Send welcome on contact connected
            if msg_type == "contactConnected":
                contact = resp.get("contact", {})
                contact_id = contact.get("contactId")
                username = contact.get("localDisplayName", "")
                if contact_id is not None:
                    target = ChatTarget(chat_id=contact_id, chat_type="direct", name=username)
                    await self.send_to(WELCOME_TEXT, target)
                    print(f"Welcomed new contact: {username}")
                return

            # Auto-accept group invitations
            if msg_type == "receivedGroupInvitation":
                group_info = resp.get("groupInfo", {})
                group_id = group_info.get("groupId")
                group_name = group_info.get("localDisplayName", "?")
                if group_id is not None:
                    await self.cmd(f"/_join #{group_id}", wait_for_response=False)
                    print(f"Auto-joined group: {group_name}")
                return

            # Send welcome when group join completes
            if msg_type == "userJoinedGroup":
                group_info = resp.get("groupInfo", {})
                group_id = group_info.get("groupId")
                group_name = group_info.get("localDisplayName", "?")
                if group_id is not None:
                    target = ChatTarget(chat_id=group_id, chat_type="group", name=group_name)
                    await self.send_to(WELCOME_TEXT, target)
                    print(f"Welcomed in group: {group_name}")
                return

            # Handle text messages (direct + group)
            if msg_type == "newChatItems":
                for item in resp.get("chatItems", []):
                    content = item.get("chatItem", {}).get("content", {})
                    if content.get("type") != "rcvMsgContent":
                        continue
                    msg_content = content.get("msgContent", {})
                    if msg_content.get("type") != "text":
                        continue

                    text = msg_content.get("text", "")
                    chat_info = item.get("chatInfo", {})
                    chat_info_type = chat_info.get("type", "")

                    if chat_info_type == "direct":
                        contact = chat_info.get("contact", {})
                        target = ChatTarget(
                            chat_id=contact.get("contactId", 0),
                            chat_type="direct",
                            name=contact.get("localDisplayName", "")
                        )
                    elif chat_info_type == "group":
                        group_info = chat_info.get("groupInfo", {})
                        target = ChatTarget(
                            chat_id=group_info.get("groupId", 0),
                            chat_type="group",
                            name=group_info.get("localDisplayName", "")
                        )
                    else:
                        continue

                    if target.chat_id == 0:
                        continue

                    await handle_command(text, target)
                return

        except Exception as e:
            print(f"Raw handler error: {e}")
            import traceback
            traceback.print_exc()


bot = RSSBot(url="ws://localhost:5225", debug=True)

WELCOME_TEXT = (
    "Welcome to SimpleX RSS Bot!\n\n"
    "I deliver RSS/Atom feed updates straight to this chat.\n\n"
    "Commands:\n\n"
    "/sub <url> [--newest]\n"
    "  Subscribe to a feed. Add --newest to get the latest post immediately.\n"
    "  Example: /sub https://hnrss.org/frontpage --newest\n\n"
    "/unsub <url>\n"
    "  Unsubscribe from a feed.\n\n"
    "/list\n"
    "  Show all active subscriptions.\n\n"
    "/help\n"
    "  Show this message again.\n\n"
    "Feeds are checked every 5 minutes. New posts are sent as they appear."
)


# --- Command handlers (work for both direct and group) ---

async def handle_command(text: str, target: ChatTarget):
    text = text.strip()
    if text.startswith("/help"):
        await bot.send_to(WELCOME_TEXT, target)
    elif text.startswith("/sub "):
        await cmd_subscribe(text, target)
    elif text.startswith("/unsub "):
        await cmd_unsubscribe(text, target)
    elif text.startswith("/list"):
        await cmd_list(target)
    elif text.startswith("/botadmin "):
        await cmd_admin(text, target)


async def cmd_admin(text: str, target: ChatTarget):
    parts = text.split(maxsplit=1)
    if not ADMIN_PASSWORD or len(parts) < 2 or parts[1].strip() != ADMIN_PASSWORD:
        return  # silent fail on wrong password

    direct = db.execute(
        "SELECT COUNT(DISTINCT chat_id) FROM subscriptions WHERE chat_type = 'direct'"
    ).fetchone()[0]
    groups = db.execute(
        "SELECT COUNT(DISTINCT chat_id) FROM subscriptions WHERE chat_type = 'group'"
    ).fetchone()[0]
    feeds = db.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
    subs = db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    erroring = db.execute("SELECT COUNT(*) FROM feeds WHERE error_count > 0").fetchone()[0]

    msg = (
        f"Bot Admin Stats\n\n"
        f"Users (direct): {direct}\n"
        f"Groups: {groups}\n"
        f"Total subscriptions: {subs}\n"
        f"Feeds tracked: {feeds}\n"
        f"Feeds with errors: {erroring}"
    )
    await bot.send_to(msg, target)


async def cmd_subscribe(text: str, target: ChatTarget):
    print(f"cmd_subscribe called: text={text!r} target={target}")
    parts = text.split()
    if len(parts) < 2:
        await bot.send_to("Usage: /sub <feed_url> [--newest]\nExample: /sub https://hnrss.org/frontpage --newest", target)
        return

    url = parts[1].strip()
    send_newest = "--newest" in parts[2:]
    print(f"Subscribing {target.chat_type}:{target.chat_id} to {url} (newest={send_newest})")

    if db.execute(
        "SELECT 1 FROM subscriptions WHERE chat_id = ? AND chat_type = ? AND feed_url = ?",
        (target.chat_id, target.chat_type, url)
    ).fetchone():
        await bot.send_to("Already subscribed to this feed.", target)
        return

    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        await bot.send_to(f"Could not parse feed: {url}", target)
        return

    title = feed.feed.get("title", url)
    newest_entry = feed.entries[0] if feed.entries else None

    db.execute("INSERT OR IGNORE INTO feeds (url, title) VALUES (?, ?)", (url, title))
    for entry in feed.entries:
        db.execute(
            "INSERT OR IGNORE INTO seen_entries (feed_url, entry_hash) VALUES (?, ?)",
            (url, entry_hash(entry))
        )
    db.execute(
        "INSERT INTO subscriptions (chat_id, chat_type, feed_url, added_at) VALUES (?, ?, ?, ?)",
        (target.chat_id, target.chat_type, url, time.time())
    )
    db.commit()
    print(f"Subscription saved: {target.chat_type}:{target.chat_id} -> {url}")

    await bot.send_to(f"Subscribed to: {title}", target)

    if send_newest and newest_entry:
        msg = format_entry(title, newest_entry)
        await bot.send_to(msg, target)


async def cmd_unsubscribe(text: str, target: ChatTarget):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await bot.send_to("Usage: /unsub <feed_url>", target)
        return

    url = parts[1].strip()

    cursor = db.execute(
        "DELETE FROM subscriptions WHERE chat_id = ? AND chat_type = ? AND feed_url = ?",
        (target.chat_id, target.chat_type, url)
    )
    db.commit()

    if not cursor.rowcount:
        await bot.send_to("Not subscribed to that feed.", target)
        return

    if not db.execute("SELECT 1 FROM subscriptions WHERE feed_url = ?", (url,)).fetchone():
        db.execute("DELETE FROM seen_entries WHERE feed_url = ?", (url,))
        db.execute("DELETE FROM feeds WHERE url = ?", (url,))
        db.commit()

    await bot.send_to("Unsubscribed.", target)


async def cmd_list(target: ChatTarget):
    rows = db.execute(
        """SELECT f.title, f.url FROM subscriptions s
           JOIN feeds f ON s.feed_url = f.url
           WHERE s.chat_id = ? AND s.chat_type = ?
           ORDER BY s.added_at""",
        (target.chat_id, target.chat_type)
    ).fetchall()

    if not rows:
        await bot.send_to("No subscriptions yet.\nUse /sub <url> to add one.", target)
        return

    lines = [f"{i+1}. {title}\n   {url}" for i, (title, url) in enumerate(rows)]
    await bot.send_to("Subscriptions:\n\n" + "\n\n".join(lines), target)


# --- Feed Monitor ---

def format_entry(feed_title: str, entry) -> str:
    title = entry.get("title", "No title")
    link = entry.get("link", "")

    raw_html = get_entry_content(entry)
    body = html_to_text(raw_html)
    if len(body) > 500:
        body = body[:500] + "..."

    media = extract_media(raw_html, entry.get("enclosures"))

    msg = f"[{feed_title}]\n\n{title}"
    if body:
        msg += f"\n\n{body}"
    if link:
        msg += f"\n\n{link}"
    if media:
        media_lines = [f"  [{m.type}] {m.url}" for m in media]
        msg += "\n\nMedia:\n" + "\n".join(media_lines)

    return msg


async def check_feeds():
    now = time.time()
    feeds = db.execute(
        "SELECT url, title, error_count, next_check FROM feeds WHERE next_check <= ?",
        (now,)
    ).fetchall()

    for feed_url, feed_title, error_count, _next_check in feeds:
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo and not feed.entries:
                raise ValueError(f"Parse failed: {feed.bozo_exception}")

            if error_count > 0:
                db.execute("UPDATE feeds SET error_count = 0 WHERE url = ?", (feed_url,))

            new_entries = []
            for entry in feed.entries:
                h = entry_hash(entry)
                if not db.execute(
                    "SELECT 1 FROM seen_entries WHERE feed_url = ? AND entry_hash = ?",
                    (feed_url, h)
                ).fetchone():
                    new_entries.append(entry)
                    db.execute(
                        "INSERT OR IGNORE INTO seen_entries (feed_url, entry_hash) VALUES (?, ?)",
                        (feed_url, h)
                    )

            db.execute("UPDATE feeds SET last_check = ? WHERE url = ?", (now, feed_url))
            db.commit()

            if not new_entries:
                continue

            subscribers = db.execute(
                "SELECT chat_id, chat_type FROM subscriptions WHERE feed_url = ?", (feed_url,)
            ).fetchall()

            for entry in reversed(new_entries):
                msg = format_entry(feed_title, entry)
                for chat_id, chat_type in subscribers:
                    target = ChatTarget(chat_id=chat_id, chat_type=chat_type)
                    try:
                        await bot.send_to(msg, target)
                    except Exception as e:
                        print(f"Send error to {chat_type}:{chat_id}: {e}")

        except Exception as e:
            new_count = error_count + 1
            backoff = min(CHECK_INTERVAL * (2 ** min(new_count, 8)), BACKOFF_CAP)
            db.execute(
                "UPDATE feeds SET error_count = ?, next_check = ? WHERE url = ?",
                (new_count, now + backoff, feed_url)
            )
            db.commit()
            print(f"[error {new_count}/{MAX_ERRORS}] {feed_url}: {e}")

            if new_count == MAX_ERRORS:
                subscribers = db.execute(
                    "SELECT chat_id, chat_type FROM subscriptions WHERE feed_url = ?", (feed_url,)
                ).fetchall()
                for chat_id, chat_type in subscribers:
                    target = ChatTarget(chat_id=chat_id, chat_type=chat_type)
                    try:
                        await bot.send_to(
                            f"Feed deactivated after {new_count} errors:\n"
                            f"{feed_title}\n{feed_url}",
                            target
                        )
                    except Exception:
                        pass


async def monitor_loop():
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        await check_feeds()


# --- Main ---

async def main():
    monitor_task = asyncio.create_task(monitor_loop())
    await bot.start_async()
    monitor_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
