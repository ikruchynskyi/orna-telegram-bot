"""Reading game announcements from a Discord channel the bot owns.

THE SETUP THIS SERVES. Official Orna announcement channels are FOLLOWED into a
channel in a server the user owns (Discord's built-in "Follow" on an
announcement channel), and the user's own bot - invited to that server - reads
that channel. No admin of the official servers is involved, and it is within
Discord's terms. telegram_announce.py relays each new announcement to Telegram.

WHY REST POLLING, NOT THE GATEWAY: one channel, a few messages a day. Polling
`GET /channels/{id}/messages?after=<last seen>` every couple of minutes needs no
persistent websocket and no discord.py - httpx is already a dependency.

ONLY FOLLOWED MESSAGES ARE RELAYED. A message that arrived through Follow
carries the IS_CROSSPOST flag (1 << 1). Anything else in that channel - the
user or friends chatting in #main - is NOT an announcement and is never sent
to Telegram.

MESSAGE CONTENT INTENT. Since 2022 Discord blanks `content`, `embeds` and
`attachments` for a bot without the privileged Message Content intent, over
REST as well as the gateway. Followed messages are authored by a WEBHOOK, so a
check that ignores bot authors (the first version of this module's did) is
blind exactly here: with the intent off it would relay nothing, forever, and
look healthy. check_intent() looks at the followed messages themselves.

Run: python3 orna_discord.py            self-check (no network, no token)
     python3 orna_discord.py discover   list servers/channels the bot can see
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import time
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
# Discord REQUIRES this exact shape for a bot's User-Agent.
USER_AGENT = "DiscordBot (https://github.com/ikruchynskyi/orna-telegram-bot, 1.0)"
DISCORD_EPOCH_MS = 1420070400000
TOKEN_ENV = "DISCORD_BOT_TOKEN"
CHANNEL_ENV = "DISCORD_ANNOUNCE_CHANNEL_ID"
PAGE = 100
IS_CROSSPOST = 1 << 1
# DEFAULT (0) and REPLY (19); joins, pins, boosts and the like carry no news.
_CONTENT_TYPES = {0, 19}


class DiscordError(RuntimeError):
    pass


class IntentMissing(DiscordError):
    """Followed messages arrive with empty content, embeds AND attachments:
    Message Content intent is off. Raised instead of relaying, so the cursor
    does not move past announcements that were never actually read."""


def snowflake_ms(snowflake) -> int:
    """Unix ms a Discord id was created at - ids encode their own timestamp."""
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


class Client:
    """Minimal synchronous Discord REST client. `transport` and `sleep` are
    injectable so _demo drives the real code through httpx.MockTransport."""

    def __init__(self, token: str, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise DiscordError(f"{TOKEN_ENV} is not set")
        self._http = httpx.Client(base_url=API_BASE, timeout=30.0, transport=transport,
                                  headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT})
        self._sleep = sleep
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Optional[dict] = None):
        for attempt in range(8):
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as e:
                if attempt >= 3:
                    raise DiscordError(f"network error on {path}: {e}") from e
                self._sleep(2 ** attempt)
                continue
            self.requests += 1
            if resp.status_code == 429:
                # Body carries the authoritative wait; the header is a backup.
                try:
                    wait = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    wait = float(resp.headers.get("Retry-After", "1"))
                self._sleep(wait + 0.25)
                continue
            if resp.status_code == 401:
                raise DiscordError("the bot token was rejected (401) - check DISCORD_BOT_TOKEN")
            if resp.status_code == 403:
                raise DiscordError(f"no access to {path} (403) - the bot needs View Channel and "
                                   "Read Message History on that channel")
            if resp.status_code == 404:
                raise DiscordError(f"{path} not found (404) - wrong channel id, or the bot is not in that server")
            if resp.status_code >= 500:
                self._sleep(min(30, 2 ** attempt))
                continue
            resp.raise_for_status()
            # When the bucket is spent, wait it out BEFORE the next call.
            if resp.headers.get("X-RateLimit-Remaining") == "0":
                self._sleep(float(resp.headers.get("X-RateLimit-Reset-After", "1")))
            return resp.json()
        raise DiscordError(f"gave up on {path} after repeated rate limits / server errors")


# ---------------------------------------------------------------------------
# Reading the channel
# ---------------------------------------------------------------------------

def is_followed(msg: dict) -> bool:
    return bool(int(msg.get("flags") or 0) & IS_CROSSPOST) and msg.get("type") in _CONTENT_TYPES


def _has_payload(msg: dict) -> bool:
    return bool((msg.get("content") or "").strip() or msg.get("embeds") or msg.get("attachments"))


def check_intent(followed: list) -> None:
    """Every followed message empty is the Message-Content-intent-off
    signature: the intent blanks content, embeds and attachments together. One
    empty message alone could be a sticker; all of them is the intent."""
    if followed and not any(_has_payload(m) for m in followed):
        raise IntentMissing(
            "followed messages arrive with EMPTY content - turn on MESSAGE CONTENT INTENT in the "
            "Discord developer portal (your application -> Bot -> Privileged Gateway Intents).")


def latest_id(client: Client, channel_id: str) -> Optional[str]:
    """The newest message id in the channel, or None if it is empty - where a
    first run starts, so it does not replay the channel's whole history."""
    page = client.get(f"/channels/{channel_id}/messages", {"limit": 1})
    return page[0]["id"] if page else None


def fetch_new(client: Client, channel_id: str, after: str) -> tuple:
    """-> (followed messages newer than `after`, oldest first; newest id seen).

    The newest id covers EVERY message, followed or not, so ordinary chat in
    the channel still advances the cursor and is not re-read each poll.
    Order-agnostic paging: the max id of a page is the next cursor."""
    out, newest = [], after
    while True:
        page = client.get(f"/channels/{channel_id}/messages", {"after": newest, "limit": PAGE})
        if not page:
            break
        newest = str(max(int(m["id"]) for m in page))
        out.extend(m for m in page if is_followed(m))
        if len(page) < PAGE:
            break
    out.sort(key=lambda m: int(m["id"]))
    check_intent(out)
    return out, newest


# ---------------------------------------------------------------------------
# Discord markup -> plain Markdown
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>|@everyone|@here")
_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_TIME_RE = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")
_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.S)
_SUBTEXT_RE = re.compile(r"^-#\s+", re.M)
_UNDERLINE_RE = re.compile(r"__(.+?)__", re.S)


def _when(match: re.Match) -> str:
    """A Discord timestamp as a fixed UTC date. Event announcements give start
    and end times ONLY in this form - "starts <t:1767225600:F>" - and every
    reader sees it in their own time zone on Discord. Telegram cannot do that,
    so it becomes an unambiguous UTC time rather than raw markup."""
    dt = datetime.datetime.fromtimestamp(int(match.group(1)), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def clean_markup(text: str) -> str:
    """Discord-only syntax that means nothing in Telegram: mentions of roles
    and channels in a server the reader is not in, custom emoji, timestamps,
    spoilers, the small "-#" subtext. Bold/italic/code/links are left as the
    Markdown they already are - telegram_go._markdown_to_html renders those."""
    text = _TIME_RE.sub(_when, text or "")
    text = _MENTION_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = _SPOILER_RE.sub(r"\1", text)
    text = _SUBTEXT_RE.sub("", text)
    text = _UNDERLINE_RE.sub(r"**\1**", text)  # Discord __underline__ -> bold; Telegram has no underline in md
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def as_announcement(msg: dict) -> dict:
    """One followed message -> {source, text, images, link}. Embeds are folded
    into the text (some announcement bots post ONLY an embed). The link points
    at the ORIGINAL message in the official server, not the copy in #main."""
    parts = [msg.get("content") or ""]
    images = [a["url"] for a in msg.get("attachments") or []
              if str(a.get("content_type", "")).startswith("image/") and a.get("url")]
    for emb in msg.get("embeds") or []:
        if emb.get("title"):
            parts.append(f"**{emb['title']}**")
        if emb.get("description"):
            parts.append(emb["description"])
        for field in emb.get("fields") or []:
            parts.append(f"**{field.get('name', '')}**\n{field.get('value', '')}")
        img = (emb.get("image") or {}).get("url")
        if img:
            images.append(img)
    ref = msg.get("message_reference") or {}
    link = (f"https://discord.com/channels/{ref['guild_id']}/{ref['channel_id']}/{ref['message_id']}"
            if ref.get("guild_id") and ref.get("channel_id") and ref.get("message_id") else "")
    author = msg.get("author") or {}
    return {"id": msg["id"], "source": author.get("username") or "Discord",
            "text": clean_markup("\n\n".join(p for p in parts if p and p.strip())),
            "images": images, "link": link}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def discover(client: Optional[Client] = None) -> None:
    """Print the servers and text channels the bot can see, so the channel id
    for DISCORD_ANNOUNCE_CHANNEL_ID can be copied rather than dug out by hand."""
    client = client or Client(os.environ.get(TOKEN_ENV, ""))
    me = client.get("/users/@me")
    print(f"bot: {me.get('username')} (application id {me.get('id')})")
    guilds = client.get("/users/@me/guilds")
    if not guilds:
        print("The bot is in no servers. Invite it to yours with View Channels + Read Message History.")
        return
    for g in guilds:
        print(f"\n=== {g['name']}  (server id {g['id']})")
        for ch in sorted(client.get(f"/guilds/{g['id']}/channels"), key=lambda c: c.get("position", 0)):
            if ch.get("type") in (0, 5):
                print(f"  {ch['id']}  #{ch.get('name')}")
    print(f"\nPut the announcements channel's id in .env:  {CHANNEL_ENV}=<id>")


def _demo() -> None:
    base = 1_300_000_000_000_000_000

    def mk(i, content="", flags=IS_CROSSPOST, embeds=None, attachments=None, mtype=0):
        return {"id": str(base + i), "type": mtype, "flags": flags, "content": content,
                "embeds": embeds or [], "attachments": attachments or [],
                "author": {"id": "9", "username": "Orna RPG #announcements", "bot": True},
                "message_reference": {"guild_id": "111", "channel_id": "222", "message_id": str(900 + i)}}

    # markup
    assert _when(re.match(r"<t:(\d+)>", "<t:1767225600>")) == "2026-01-01 00:00 UTC"
    cleaned = clean_markup("<@&123> Event starts <t:1767225600:F>! <:orns:555> ||secret|| __big__\n-# small")
    assert cleaned == "Event starts 2026-01-01 00:00 UTC!  secret **big**\nsmall", repr(cleaned)
    assert clean_markup("Use code `ORNA2026` at https://playorna.com/x") == \
        "Use code `ORNA2026` at https://playorna.com/x", "code spans and URLs untouched"

    # only followed messages count
    assert is_followed(mk(1, "hi")) and not is_followed(mk(2, "my own chat", flags=0))
    assert not is_followed(mk(3, "pinned", mtype=6)), "system messages are not news"

    # embeds folded in, images collected, link points at the ORIGINAL
    a = as_announcement(mk(4, "Patch notes are out", embeds=[{"title": "v3.27", "description": "Fixes",
                                                              "image": {"url": "https://cdn/e.png"}}],
                           attachments=[{"url": "https://cdn/a.png", "content_type": "image/png"},
                                        {"url": "https://cdn/f.zip", "content_type": "application/zip"}]))
    assert a["text"] == "Patch notes are out\n\n**v3.27**\n\nFixes", repr(a["text"])
    assert a["images"] == ["https://cdn/a.png", "https://cdn/e.png"]
    assert a["link"] == "https://discord.com/channels/111/222/904"

    # intent: all followed messages empty -> refuse; one real one -> fine
    try:
        check_intent([mk(5), mk(6)])
    except IntentMissing:
        pass
    else:
        raise AssertionError("all-empty followed messages must raise IntentMissing")
    check_intent([mk(5), mk(7, "real")])
    check_intent([])

    # fetch_new through the real client: ordinary chat advances the cursor but
    # is never returned; pages are walked; result is oldest first.
    chat = [mk(i, f"followed {i}") if i % 3 else mk(i, "chatting", flags=0) for i in range(1, 151)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bot T"
        after = int(dict(request.url.params).get("after", 0))
        newer = sorted((m for m in chat if int(m["id"]) > after), key=lambda m: int(m["id"]))[:PAGE]
        return httpx.Response(200, json=list(reversed(newer)))  # Discord: newest first

    c = Client("T", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    got, newest = fetch_new(c, "1", str(base))
    assert newest == str(base + 150), "cursor moves past ordinary chat too"
    assert len(got) == 100 and all(m["flags"] & IS_CROSSPOST for m in got), len(got)
    assert [int(m["id"]) for m in got] == sorted(int(m["id"]) for m in got), "oldest first"
    assert c.requests == 2, "150 messages = two pages"
    assert fetch_new(c, "1", newest) == ([], newest), "nothing new -> nothing returned"
    print("orna_discord: all checks passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        discover()
    else:
        _demo()
