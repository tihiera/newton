"""Reading a paper longer than the reader's context: notes on every part (several at a
time), merged level by level until they fit, then the card from the notes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from newton_agentd.research import reading


def test_parts_are_cut_at_paragraphs_and_cover_the_text() -> None:
    text = "".join(f"Paragraph {i} says something.\n\n" for i in range(300))
    parts = reading.split(text, 1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 5
    assert all(p.startswith("Paragraph") and p.endswith(".") for p in parts)
    assert "".join(p.replace("\n\n", "") for p in parts) == text.replace("\n\n", "")
    assert reading.split("x" * 2500, 1000) == ["x" * 1000, "x" * 1000, "x" * 500]  # no breaks


def test_groups_pair_up_consecutive_notes() -> None:
    notes = ["a" * 400] * 5
    assert [len(g) for g in reading.groups(notes, 1000)] == [2, 3]  # never a lone note
    assert reading.groups(["a"], 1000) == [["a"]]


def test_a_part_fills_the_context_to_80_percent() -> None:
    size = reading.part_size(8192, 60_000)
    room = (8192 - reading.NOTE_TOKENS - reading.CONTEXT_MARGIN) * 3 - len(reading.NOTES) - 300
    assert size == int(room * 0.8)
    assert reading.part_size(None, 60_000) == 48_000
    assert reading.part_size(262_144, 60_000) == 48_000  # never more than one call reads


class Router:
    """Answers notes and merges; counts how many calls run at once."""

    def __init__(self, slots: int, notes: str = "n" * 600, fail: set[int] | None = None) -> None:
        self._slots = slots
        self.notes = notes
        self.fail = fail or set()
        self.running = 0
        self.most = 0
        self.merges: list[str] = []
        self.calls = 0

    def slots(self, model: str) -> int:
        return self._slots

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.running += 1
        self.most = max(self.most, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        system, user = (m["content"] for m in payload["messages"])
        assert payload["reasoning_effort"] == "none" and payload["temperature"] == 0
        if system.startswith("These are notes"):
            self.merges.append(user)
            content = "merged"
        else:
            part = int(system.split("This is part ")[1].split(" ")[0])
            if part in self.fail:
                raise RuntimeError("the service is down")
            content = self.notes
        return {"body": {"choices": [{"message": {"content": content}}]}, "provenance": {}}


async def test_notes_are_taken_in_parallel_and_merged_until_they_fit() -> None:
    router = Router(slots=3)
    text = "".join(f"Paragraph {i}.\n\n" for i in range(2000))
    reader = reading.Reader(router, "m", "A paper")
    notes, stats = await reader.condense(text, room=2000, size=1500)
    assert stats["read_in_parts"] > 15 and router.most == 3  # never more than its slots
    assert stats["merge_levels"] >= 1 and len(notes) <= 2000
    assert all(m.count("[Part") >= 2 for m in router.merges[:5])  # pairs at least
    assert stats["model_calls"] == router.calls


async def test_notes_that_fit_are_not_merged() -> None:
    router = Router(slots=1, notes="short")
    notes, stats = await reading.Reader(router, "m", "T").condense("x. " * 3000, 10_000, 4000)
    assert stats["merge_levels"] == 0 and router.merges == []
    assert notes.startswith("[Part 1 of 3] short")


async def test_a_failing_part_is_tried_again_then_skipped() -> None:
    router = Router(slots=2, notes="ok", fail={2})
    notes, stats = await reading.Reader(router, "m", "T").condense("y. " * 3000, 10_000, 4000)
    assert "[Part 2 of 3]" not in notes and "[Part 3 of 3] ok" in notes
    assert stats["model_calls"] == 4  # part 2 twice
    router = Router(slots=2, notes="ok", fail={1, 2})
    with pytest.raises(reading.ReadingError, match="only 1 of the paper's 3 parts"):
        await reading.Reader(router, "m", "T").condense("y. " * 3000, 10_000, 4000)
