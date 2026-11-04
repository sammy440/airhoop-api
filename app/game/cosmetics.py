"""
Unlock levels for ball skins and trails. Mirrors BALL_SKINS / TRAILS in the frontend's
src/lib/game/cosmetics.ts (tests/test_xp.py and cosmetics' frontend test pin the same table).
The server only relays a loadout the player has actually unlocked.
"""

from __future__ import annotations

from typing import Any

BALL_LEVELS = {"classic": 1, "retro": 3, "gold": 6, "ice": 10, "lava": 15, "galaxy": 22}
TRAIL_LEVELS = {"flame": 1, "blue": 4, "toxic": 8, "royal": 12, "rainbow": 18}
DEFAULT_LOADOUT = {"ball": "classic", "trail": "flame"}


def usable_loadout(loadout: dict[str, Any] | None, level: int) -> dict[str, str]:
    """The loadout as others may see it: anything unknown or still locked at `level` falls back to the default."""
    loadout = loadout or {}
    ball = loadout.get("ball")
    trail = loadout.get("trail")
    return {
        "ball": ball if isinstance(ball, str) and BALL_LEVELS.get(ball, 999) <= level else DEFAULT_LOADOUT["ball"],
        "trail": trail if isinstance(trail, str) and TRAIL_LEVELS.get(trail, 999) <= level else DEFAULT_LOADOUT["trail"],
    }
