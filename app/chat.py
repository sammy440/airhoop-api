"""
Room chat. Nothing here is stored: a message is checked, sent to everyone in the room, and
forgotten. No database rows, no log lines with the text, no history kept in the room (a player
who reconnects doesn't get old messages back).

- Rooms joined with a code (friends): free text, cleaned up (`clean_text`).
- Quick-match rooms (strangers): quick chat only, a fixed set of phrases sent by id
  (`QUICK_CHAT`), the same safe-by-design approach as reactions.
"""

from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 200
CHAT_BURST = 5  # at most this many messages …
CHAT_WINDOW_S = 10.0  # … per player in this window

# Quick chat: ids on the wire; the client shows the words.
QUICK_CHAT = ("gg", "nice_shot", "rematch", "one_more", "good_luck", "unlucky", "wow", "thanks")

# Links and contact details are removed: chat isn't for sending people elsewhere.
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|gg|co|me|ly|app|xyz|info|biz|tv|link|to|uk|ng)\b(?:/\S*)?")
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Mild, common profanity: masked, not blocked (first letter kept: "f***").
_PROFANITY = (
    "fuck", "fucking", "fucker", "shit", "bitch", "bastard", "cunt", "dick", "pussy", "asshole",
    "slut", "whore", "nigger", "nigga", "faggot", "retard", "motherfucker", "wanker", "twat",
)
_PROFANE = re.compile(r"(?i)\b(" + "|".join(sorted(_PROFANITY, key=len, reverse=True)) + r")\b")


def clean_text(text: object) -> str | None:
    """A message fit to pass on, or None if nothing's left. Control characters go, whitespace collapses,
    links and emails become [link] / [hidden], profanity is masked, and it's cut to MAX_CHARS."""
    if not isinstance(text, str):
        return None
    # No control / formatting characters (zero-width tricks, bidi overrides, newlines)
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    text = " ".join(text.split())
    text = _EMAIL.sub("[hidden]", text)
    text = _URL.sub("[link]", text)
    text = _PROFANE.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text)
    text = text[:MAX_CHARS].strip()
    return text or None
