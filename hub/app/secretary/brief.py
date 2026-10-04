"""The daily brief (contract §1 B) on the brief model (Sonnet 5.5 by default).

The material is C1's day material (condense.brief_material, already redacted): sessions with their
digests, shipped work, things in progress, decisions, what waits on the owner, follow-ups, commit counts
and notes. The answer is three texts; the chat text is posted by notify.py, which knows the chat kind,
so it is plain text with no mentions.
"""

from __future__ import annotations

import json
import re

from .. import config
from . import llm, prompts

MAX_MATERIAL_CHARS = 150_000
CHAT_CHARS = 1900  # Discord's 2000 less a session label


def fit(material: dict, limit: int = MAX_MATERIAL_CHARS) -> dict:
    """Drop the longest lists' tails until the JSON fits (a busy day; the counts stay whole)."""
    m = dict(material)
    for key in ("sessions", "followups", "needs_you", "shipped", "decisions", "in_progress", "notes", "hygiene"):
        while isinstance(m.get(key), list) and len(m[key]) > 3 and \
                len(json.dumps(m, ensure_ascii=False, default=str)) > limit:
            m[key] = m[key][: max(3, len(m[key]) * 2 // 3)]
    return m


def _one_line(v, n: int) -> str:
    s = " ".join(str(v or "").split()).rstrip(".")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _chat(v) -> str:
    text = str(v or "").strip()
    text = re.sub(r"@(everyone|here)\b", r"\1", text)  # never ping a channel
    text = re.sub(r"<@[!&]?\d+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if len(text) <= CHAT_CHARS else text[: CHAT_CHARS - 1].rstrip() + "…"


async def daily_brief(material: dict) -> dict:
    """{headline, lede, discord_text, usage}. Raises llm.ModelError when no usable brief came back."""
    if not isinstance(material, dict):
        raise llm.ModelBadOutput("brief failed: the material is not an object")
    msg = prompts.material_message("The day's material", fit(material), "Write the brief for this day.")
    owner = material.get("owner") or config.OWNER
    data, use = await llm.structured(config.BRIEF_MODEL, prompts.brief_system(owner), msg,
                                     prompts.BRIEF_SCHEMA, timeout_s=llm.BRIEF_TIMEOUT_S, what="brief")
    out = {"headline": _one_line(data.get("headline"), 160),
           "lede": " ".join(str(data.get("lede") or "").split())[:2000],
           "discord_text": _chat(data.get("discord_text")),
           "usage": use}
    if not out["headline"] and not out["lede"]:
        raise llm.ModelBadOutput("brief failed: the answer was empty", use)
    return out
