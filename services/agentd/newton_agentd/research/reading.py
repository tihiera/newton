"""Reading a paper longer than the reader's context: map, then reduce.

A paper that fits is read in one call, as before. A longer one is cut into parts that
fill the context to 80 % (the rest is room for tokenizers that count differently), and:

  map:     each part becomes short notes on what the card needs (method, claims, tests),
           several parts at a time (as many as the reader's services have slots);
  reduce:  consecutive notes are merged, level by level, like a parallel reduction, until
           all of them fit next to the card prompt.

The card is then written from the notes and the abstract, so the whole paper is read,
not only its first pages. The notes are plain text: only the card is structured, and it
is checked field by field as always (parse_card).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import structured

log = logging.getLogger("newton_agentd.reading")

FILL = 0.8  # of the context a part may take
NOTE_TOKENS = 400  # one part's notes (max_tokens)
MERGE_TOKENS = 500  # merged notes (max_tokens)
CHARS_PER_TOKEN = 3  # as papers.CHARS_PER_TOKEN: LaTeX-heavy text
CONTEXT_MARGIN = 256
MAX_PARTS = 64  # a paper this long is read up to here (and the provenance says so)
MAX_LEVELS = 6
MAX_CONCURRENT = 8

NOTES = """You take notes on one part of a numerical-methods paper, for a reader who will later
describe the paper from your notes alone. This is part %(i)d of %(n)d of "%(title)s".

Write plain notes (no JSON, at most 180 words) with only what THIS part states about:
- the method proposed: its name, flux or reconstruction, limiter, time integration
- the claimed order of accuracy, CFL or stability limit, TVD or monotonicity, conservation
- other claims (accuracy, speed, ...) and the test problems used
Copy numbers exactly. Write "nothing on these points" if this part has none."""

MERGE = """These are notes on consecutive parts of the numerical-methods paper "%(title)s".
Merge them into one set of notes (plain text, at most 250 words): keep every fact about the
method, its claims (order, CFL, TVD, conservation, accuracy, speed) and the test problems;
drop repeats and "nothing on these points"; never add anything the notes don't say."""


class ReadingError(ValueError):
    """Too few parts of the paper could be read."""


def split(text: str, size: int) -> list[str]:
    """Parts of at most `size` characters, cut at a paragraph, else a line, else a
    sentence, never in the first half of a part."""
    parts: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind("\n\n", size // 2, size)
        if cut < 0:
            cut = rest.rfind("\n", size // 2, size)
        if cut < 0:
            dot = rest.rfind(". ", size // 2, size)
            cut = dot + 1 if dot >= 0 else size
        parts.append(rest[:cut].strip())
        rest = rest[cut:]
    parts.append(rest.strip())
    return [p for p in parts if p]


def part_size(context: int | None, fallback: int) -> int:
    """Characters of paper per notes call: 80 % of what fits next to the notes prompt."""
    if not context:
        return int(fallback * FILL)
    room = (context - NOTE_TOKENS - CONTEXT_MARGIN) * CHARS_PER_TOKEN - len(NOTES) - 300
    return int(min(room, fallback) * FILL)


def groups(notes: list[str], size: int) -> list[list[str]]:
    """Consecutive notes in groups of at least two whose text fits in `size`."""
    out: list[list[str]] = []
    current: list[str] = []
    for note in notes:
        if len(current) >= 2 and sum(map(len, current)) + len(note) > size:
            out.append(current)
            current = []
        current.append(note)
    if current:
        if len(current) == 1 and out:
            out[-1].append(current[0])
        else:
            out.append(current)
    return out


class Reader:
    def __init__(self, router: Any, model: str, title: str) -> None:
        self.router = router
        self.model = model
        self.title = title[:200]
        slots = getattr(router, "slots", None)
        n = slots(model) if slots is not None else 1
        self.gate = asyncio.Semaphore(max(1, min(MAX_CONCURRENT, n)))
        self.calls = 0

    async def ask(self, system: str, user: str, max_tokens: int) -> str:
        """One plain-text answer; a failure or an empty answer is tried once more."""
        for attempt in (1, 2):
            async with self.gate:
                self.calls += 1
                try:
                    result = await self.router.complete({
                        "model": self.model, "temperature": 0, "max_tokens": max_tokens,
                        **structured.NO_THINKING,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": user}],
                    })  # fmt: skip
                except Exception as e:  # noqa: BLE001 - a busy or failing service: once more
                    log.info("reading %r: call failed (%s), attempt %d", self.title[:60],
                             type(e).__name__, attempt)  # fmt: skip
                    continue
            choice = (result["body"].get("choices") or [{}])[0]
            text = str((choice.get("message") or {}).get("content") or "").strip()
            if text:
                return text
        return ""

    async def condense(self, text: str, room: int, size: int) -> tuple[str, dict[str, Any]]:
        """Notes on the whole text, merged until they fit in `room` characters."""
        parts = split(text, size)
        capped = len(parts) > MAX_PARTS
        parts = parts[:MAX_PARTS]
        n = len(parts)
        notes = await asyncio.gather(*(
            self.ask(NOTES % {"i": i + 1, "n": n, "title": self.title}, part, NOTE_TOKENS)
            for i, part in enumerate(parts)
        ))  # fmt: skip
        read = [f"[Part {i + 1} of {n}] {note}" for i, note in enumerate(notes) if note]
        if len(read) * 2 < n:
            raise ReadingError(f"only {len(read)} of the paper's {n} parts could be read")
        levels = 0
        while len("\n\n".join(read)) > room and len(read) > 1 and levels < MAX_LEVELS:
            batches = groups(read, size)
            if len(batches) >= len(read):  # nothing to pair up: can't shrink further
                break
            merged = await asyncio.gather(*(
                self.ask(MERGE % {"title": self.title}, "\n\n".join(batch), MERGE_TOKENS)
                for batch in batches
            ))  # fmt: skip
            # A merge that failed keeps its notes as they were (cut below if need be).
            read = [m or "\n\n".join(b) for m, b in zip(merged, batches)]
            levels += 1
        joined = "\n\n".join(read)[:room]
        stats = {"read_in_parts": n, "merge_levels": levels, "model_calls": self.calls,
                 "characters_read": sum(map(len, parts))}  # fmt: skip
        if capped:
            stats["parts_skipped"] = True
        return joined, stats
