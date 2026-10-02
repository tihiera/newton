"""Keywords for a research goal, from its title and description: what Newton searches arXiv
for. The reader model proposes a few short search phrases; when it can't (no reader
running, a bad answer), the distinctive terms of the text itself are used. Every keyword
is checked against the goal's keyword rule (letters, digits, spaces, .+'-) before use, so
nothing the model writes reaches the search unchecked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger("newton_agentd.keywords")

KEYWORD = re.compile(r"^[A-Za-z0-9 .+'-]{2,60}$")
MAX_KEYWORDS = 6
MODEL_TIMEOUT = 45.0

PROMPT = """You choose arXiv search keywords for a researcher. Their research topic:

  %(topic)s

Answer with ONE JSON object and nothing else:
{"keywords": ["...", "..."]}

3 to 6 keywords: short phrases of 1 to 4 words, the technical terms a paper on this topic
would use in its abstract (methods, equations, properties). English, no quotes, no
boolean operators."""

_STOP = frozenset(
    """a an and are as at be been but by can could do does for from has have how if in into is
    it its may might more most of on or our over should so such than that the their them then
    there these they this those to under up use used using via was we were what when where
    which while who why will with within without would vs versus between both each other
    investigate investigating study studying explore exploring find finding validate
    validating compare comparing especially although whether new novel approach approaches
    beat beats beating improve improves improving make makes reach reaches""".split()  # noqa: SIM905
)


def clean(raw: Any) -> list[str]:
    """The valid, distinct keywords of a list (en dashes as hyphens), at most six."""
    out: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        k = " ".join(item.replace("–", "-").replace("—", "-").split()).strip(" .")
        if KEYWORD.match(k) and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def norm(text: str) -> str:
    """En and em dashes as hyphens (Rhie–Chow -> Rhie-Chow)."""
    return text.replace("–", "-").replace("—", "-")


def from_text(title: str, description: str) -> list[str]:
    """Distinctive terms of the topic, without a model: the title's phrases (its runs of
    words between filler words), then names and acronyms of the text (BDF2, Rhie-Chow,
    OpenFOAM)."""
    runs: list[str] = []
    current: list[str] = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+'-]*", norm(title)):
        if w.lower() in _STOP:
            if current:
                runs.append(" ".join(current))
            current = []
        else:
            current.append(w.lower())
    if current:
        runs.append(" ".join(current))
    phrases = []
    for run in runs:
        words = run.split()
        # Long runs as pairs: an abstract rarely has five words in that exact order.
        phrases += [run] if len(words) <= 3 else [" ".join(p) for p in zip(words, words[1:])]
    names = []
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+'-]*", norm(f"{title} {description}")):
        w = re.sub(r"-(style|based|like|type)$", "", w)
        named = re.search(r"[a-z0-9][A-Z]|[A-Z]{2}|-[A-Z]", w)  # BDF2, OpenFOAM, Rhie-Chow
        mixed = re.search(r"[A-Za-z]\d|\d[A-Za-z]{2}", w)
        if (named or mixed) and not re.fullmatch(r"\d+(st|nd|rd|th)(-\w+)?", w):  # not "2nd"
            names.append(w)
    return clean(phrases + names)


async def suggest(router: Any, model: str | None, title: str, description: str
                  ) -> tuple[list[str], str]:  # fmt: skip
    """(keywords, "model" | "text"): the reader's proposal, else the text's own terms."""
    if model:
        topic = f"{title}. {description}".strip()[:2000]
        try:
            result = await asyncio.wait_for(router.complete({
                "model": model, "temperature": 0, "max_tokens": 200,
                "messages": [{"role": "user", "content": PROMPT % {"topic": topic}}],
            }), MODEL_TIMEOUT)  # fmt: skip
            content = ((result["body"].get("choices") or [{}])[0].get("message") or {}).get(
                "content"
            ) or ""
            match = re.search(r"\{.*\}", content, re.S)
            answer = json.loads(match.group(0)) if match else {}
            keywords = clean(answer.get("keywords") if isinstance(answer, dict) else None)
            if len(keywords) >= 2:
                return keywords, "model"
        except Exception as e:  # noqa: BLE001 - no reader running, a timeout, a bad answer
            reason = type(e).__name__
            log.info("keywords from the model failed (%s): using the text's terms", reason)
    return from_text(title, description), "text"
