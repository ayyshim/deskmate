"""A session's digest (contract §1 D) on the digest model (Haiku 4.5 by default).

The material is C1's condensed session (condense.session_delta, already redacted). It is sent as JSON in
the user message; when it is very long the oldest turns' texts are cut first, so the newest work and the
lists of files, commands and commits always fit.
"""

from __future__ import annotations

import copy
import json

from .. import config
from . import llm, prompts

MAX_MATERIAL_CHARS = 120_000  # about 30k tokens, a few cents on Haiku at most
MIN_TEXT = 200


def _size(m: dict) -> int:
    return len(json.dumps(m, ensure_ascii=False, default=str))


def fit(material: dict, limit: int = MAX_MATERIAL_CHARS) -> dict:
    """The material, shortened to `limit` characters of JSON: oldest turns' answers and prompts first."""
    if _size(material) <= limit:
        return material
    m = copy.deepcopy(material)
    turns = ((m.get("delta") or {}).get("turns")) or []
    for cap in (1200, 600, MIN_TEXT):
        for t in turns[:-3] if len(turns) > 3 else turns:
            for k in ("answer", "prompt"):
                v = t.get(k)
                if isinstance(v, str) and len(v) > cap:
                    t[k] = v[: cap - 1] + "…"
            if isinstance(t.get("mid_turn"), list):
                t["mid_turn"] = [s[:cap] for s in t["mid_turn"] if isinstance(s, str)][:3]
            if _size(m) <= limit:
                return m
    while len(turns) > 1 and _size(m) > limit:  # still too big: drop the oldest turns
        turns.pop(0)
        m["delta"]["turns_dropped"] = int(m["delta"].get("turns_dropped") or 0) + 1
    return m


def _line(v, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _lines(v, n: int = 400, limit: int = 12) -> list[str]:
    return [x for x in (_line(i, n) for i in (v or []) if isinstance(i, (str, int, float))) if x][:limit]


def clean(data: dict, material: dict) -> dict:
    """Types and lengths fixed, outcome 'unclear' → None, repos only from the material."""
    known = [r for r in (material.get("repos") or []) if isinstance(r, str)]
    labels = material.get("repo_labels") or {}
    by_label = {str(v).lower(): k for k, v in labels.items() if isinstance(v, str)}
    repos = []
    for r in data.get("repos") or []:
        key = r if r in known else by_label.get(str(r).lower())
        if key and key not in repos:
            repos.append(key)
    loops = []
    for x in data.get("open_loops") or []:
        if isinstance(x, dict) and _line(x.get("text"), 400):
            loops.append({"text": _line(x["text"], 400),
                          "owner": x.get("owner") if x.get("owner") in prompts.OWNERS else "you"})
    outcome = data.get("outcome")
    return {
        "title": _line(data.get("title"), 120) or _line(material.get("title_hint"), 120) or "Session",
        "repos": repos,
        "outcome": outcome if outcome in prompts.OUTCOMES else None,
        "summary": _line(data.get("summary"), 1000),
        "shipped": _lines(data.get("shipped")),
        "decisions": _lines(data.get("decisions")),
        "open_loops": loops[:12],
        "blockers": _lines(data.get("blockers")),
    }


async def digest_session(condensed: dict) -> dict:
    """{title, repos, outcome, summary, shipped, decisions, open_loops: [{text, owner}], blockers, usage}.

    Raises llm.ModelError (a subclass naming the cause) when no usable digest came back."""
    if not isinstance(condensed, dict):
        raise llm.ModelBadOutput("digest failed: the material is not an object")
    material = fit(condensed)
    msg = prompts.material_message("Session material", material, "Write the digest of this session.")
    owner = condensed.get("owner") or config.OWNER
    data, use = await llm.structured(config.DIGEST_MODEL, prompts.digest_system(owner), msg,
                                     prompts.DIGEST_SCHEMA, timeout_s=llm.DIGEST_TIMEOUT_S, what="digest")
    if not _line(data.get("title"), 120) and not _line(data.get("summary"), 1000):
        raise llm.ModelBadOutput("digest failed: the answer was empty", use)
    out = clean(data, condensed)
    out["usage"] = use
    return out
