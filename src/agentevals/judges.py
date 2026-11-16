"""ADK judge evaluators that keep the judge's reasoning next to its score."""

from __future__ import annotations

import json
import re

from google.adk.evaluation.eval_rubrics import RubricScore
from google.adk.evaluation.final_response_match_v2 import FinalResponseMatchV2Evaluator
from google.adk.evaluation.llm_as_judge import AutoRaterScore
from google.adk.evaluation.llm_as_judge_utils import get_text_from_content
from google.adk.models.llm_response import LlmResponse

FINAL_RESPONSE_MATCH_RUBRIC_ID = "final_response_match"

_REASONING_KEY = re.compile(r'"reasoning"\s*:\s*')


def parse_judge_reasoning(response_text: str) -> str | None:
    """Return the ``reasoning`` value from a judge reply, or None.

    ADK's prompt asks for JSON with a trailing comma and an unquoted label placeholder, so judges
    often reply with text ``json.loads`` rejects. Decoding only the value after the key tolerates
    that, along with code fences and prose around the object.
    """
    match = _REASONING_KEY.search(response_text)
    if match is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(response_text, match.end())
    except json.JSONDecodeError:
        return None
    if isinstance(value, list):
        value = "\n".join(item for item in value if isinstance(item, str))
    if not isinstance(value, str):
        return None
    return value.strip() or None


class FinalResponseMatchV2WithReasoning(FinalResponseMatchV2Evaluator):
    """``final_response_match_v2`` that also returns the judge's reasoning as a rubric score.

    The score comes from ADK's own label parsing and is never changed by the reasoning. The
    rationale rides on ``rubric_scores`` of each sample, and the majority vote returns a whole
    winning sample, so the reasoning reported is the one behind the reported verdict.

    Remove once ADK returns the judge reasoning for this metric itself.
    """

    def convert_auto_rater_response_to_score(self, llm_response: LlmResponse) -> AutoRaterScore:
        auto_rater_score = super().convert_auto_rater_response_to_score(llm_response)
        if auto_rater_score.score is None:
            return auto_rater_score
        reasoning = parse_judge_reasoning(get_text_from_content(llm_response.content) or "")
        if reasoning is None:
            return auto_rater_score
        return AutoRaterScore(
            score=auto_rater_score.score,
            rubric_scores=[
                RubricScore(
                    rubric_id=FINAL_RESPONSE_MATCH_RUBRIC_ID,
                    rationale=reasoning,
                    score=auto_rater_score.score,
                )
            ],
        )
