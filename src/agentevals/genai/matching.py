"""Select the expected conversation (golden case) for an actual conversation and align turns.

Selection is explicit and never guesses:

1. an explicit case id (``agentevals.eval.case.id`` on the resource or anchor span);
2. otherwise the normalized first user text (casefolded, whitespace collapsed);
3. otherwise no case, with a reason. There is no fallback to the first case.

When several cases share the first user text, the one with the same turn count wins; if that
still leaves more than one, the match is ambiguous.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .messages import text_of
from .model import Conversation, Turn

EVAL_CASE_ID = "agentevals.eval.case.id"

_WS = re.compile(r"\s+")


def normalize_text(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip().casefold()


@dataclass(frozen=True, slots=True)
class ExpectedTurn:
    user_text: str | None
    final_text: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(slots=True)
class ExpectedConversation:
    case_id: str
    turns: list[ExpectedTurn]
    raw: Any = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    case: ExpectedConversation | None
    method: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class Alignment:
    pairs: list[tuple[Turn, ExpectedTurn]] = field(default_factory=list)
    missing_turns: int = 0
    unexpected_turns: int = 0


def select_case(
    conversation: Conversation,
    cases: Sequence[ExpectedConversation],
    explicit_case_id: str | None = None,
) -> MatchResult:
    if explicit_case_id:
        for case in cases:
            if case.case_id == explicit_case_id:
                return MatchResult(case, method="explicit")
        return MatchResult(None, reason=f"eval case {explicit_case_id!r} not found")

    if not conversation.turns:
        return MatchResult(None, reason="no turns to match")
    first_user = normalize_text(text_of(conversation.turns[0].user_input))
    if not first_user:
        return MatchResult(None, reason="no user input to match")

    candidates = [c for c in cases if c.turns and normalize_text(c.turns[0].user_text) == first_user]
    if not candidates:
        return MatchResult(None, reason="no matching eval case")
    if len(candidates) > 1:
        same_length = [c for c in candidates if len(c.turns) == len(conversation.turns)]
        if len(same_length) != 1:
            return MatchResult(None, reason="ambiguous eval case")
        candidates = same_length
    return MatchResult(candidates[0], method="user_text")


def align(actual: Sequence[Turn], expected: Sequence[ExpectedTurn]) -> Alignment:
    """Pair turns by index and count the turns present on only one side."""
    n = min(len(actual), len(expected))
    return Alignment(
        pairs=list(zip(actual[:n], expected[:n], strict=True)),
        missing_turns=max(0, len(expected) - len(actual)),
        unexpected_turns=max(0, len(actual) - len(expected)),
    )
