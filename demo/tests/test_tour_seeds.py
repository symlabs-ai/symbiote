"""The tour's dream step shows the prune removing two stale memories. A
memory that enters a recall counts as used and escapes the prune, so the
seeds meant for it must share no term with what the tour sends in chat."""

from __future__ import annotations

import re
from pathlib import Path

from demo.seed import DREAM_SEEDS
from symbiote.memory.recall import terms

TOUR = Path(__file__).resolve().parents[1] / "static" / "js" / "tour.js"


def tour_prompts() -> list[str]:
    source = TOUR.read_text(encoding="utf-8")
    return [p.replace("\\'", "'") for p in re.findall(r"prompt: '((?:[^'\\]|\\.)*)'", source)]


def test_every_tour_step_with_chat_is_read() -> None:
    assert len(tour_prompts()) >= 12


def test_prune_seeds_share_no_term_with_the_tour_prompts() -> None:
    prune = [(content, tags) for _type, content, tags, importance, days in DREAM_SEEDS
             if days * (1 - importance) > 30]
    assert len(prune) == 2
    for content, tags in prune:
        seed_terms = terms(content).union(*(terms(t) for t in tags))
        for prompt in tour_prompts():
            shared = seed_terms & terms(prompt)
            assert not shared, f"{content!r} would be recalled by {prompt!r} through {shared}"
