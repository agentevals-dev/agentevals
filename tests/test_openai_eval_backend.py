import pytest
from unittest.mock import MagicMock

from agentevals.config import OpenAIEvalDef
from agentevals.openai_eval_backend import (
    _build_jsonl_items,
    _build_testing_criteria,
    evaluate_openai_eval,
)


def _score_grader(**overrides):
    base = {
        "type": "score_model",
        "model": "gpt-4o-mini",
        "input": [{"role": "user", "content": "Rate: {{ item.actual_response }}"}],
    }
    base.update(overrides)
    return base


def _invocation(text: str):
    inv = MagicMock()
    inv.final_response.parts = [MagicMock(text=text)]
    return inv


class TestOpenAIEvalDefValidation:
    def test_text_similarity_valid(self):
        d = OpenAIEvalDef(name="sim", grader={"type": "text_similarity", "evaluation_metric": "bleu"})
        assert d.grader["type"] == "text_similarity"

    def test_text_similarity_missing_metric(self):
        with pytest.raises(Exception, match="evaluation_metric"):
            OpenAIEvalDef(name="sim", grader={"type": "text_similarity"})

    def test_text_similarity_bad_metric(self):
        with pytest.raises(Exception, match="Unknown evaluation_metric"):
            OpenAIEvalDef(name="sim", grader={"type": "text_similarity", "evaluation_metric": "invalid"})

    def test_score_model_valid(self):
        d = OpenAIEvalDef(name="sc", grader=_score_grader())
        assert d.grader["type"] == "score_model"

    @pytest.mark.parametrize("field", ["model", "input"])
    def test_score_model_missing_required_field(self, field):
        with pytest.raises(Exception, match=field):
            OpenAIEvalDef(name="sc", grader=_score_grader(**{field: None}))

    def test_unsupported_grader_type(self):
        with pytest.raises(Exception, match="Unsupported grader type"):
            OpenAIEvalDef(name="x", grader={"type": "unknown"})


class TestBuildTestingCriteria:
    def test_text_similarity_shape(self):
        d = OpenAIEvalDef(name="sim", grader={"type": "text_similarity", "evaluation_metric": "bleu"}, threshold=0.7)
        c = _build_testing_criteria(d)
        assert c["type"] == "text_similarity"
        assert c["evaluation_metric"] == "bleu"
        assert c["pass_threshold"] == 0.7
        assert "{{ item.actual_response }}" in c["input"]
        assert "{{ item.expected_response }}" in c["reference"]

    def test_score_model_shape(self):
        grader = _score_grader(range=[1, 5])
        d = OpenAIEvalDef(name="sc", grader=grader, threshold=3.0)
        c = _build_testing_criteria(d)
        assert c["type"] == "score_model"
        assert c["model"] == "gpt-4o-mini"
        assert c["range"] == [1, 5]
        assert c["pass_threshold"] == 3.0
        assert c["input"] == grader["input"]

    def test_score_model_default_range(self):
        d = OpenAIEvalDef(name="sc", grader=_score_grader())
        c = _build_testing_criteria(d)
        assert c["range"] == [0, 1]


class TestBuildJsonlItems:
    def test_text_similarity_includes_expected(self):
        items = _build_jsonl_items([_invocation("hello")], [_invocation("world")], include_expected=True)
        assert "expected_response" in items[0]["item"]

    def test_score_model_excludes_expected(self):
        items = _build_jsonl_items([_invocation("hello")], [], include_expected=False)
        assert "expected_response" not in items[0]["item"]

    def test_missing_expected_falls_back_to_empty(self):
        items = _build_jsonl_items([_invocation("hello")], [], include_expected=True)
        assert items[0]["item"]["expected_response"] == ""


class TestEvaluateOpenAIEval:
    async def test_no_api_key_returns_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        d = OpenAIEvalDef(name="sim", grader={"type": "text_similarity", "evaluation_metric": "bleu"})
        result = await evaluate_openai_eval(d, [], [])
        assert "OPENAI_API_KEY" in (result.error or "")

    async def test_text_similarity_requires_expected(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        d = OpenAIEvalDef(name="sim", grader={"type": "text_similarity", "evaluation_metric": "bleu"})
        result = await evaluate_openai_eval(d, [_invocation("hi")], None)
        assert "expected invocations" in (result.error or "")

    async def test_score_model_does_not_require_expected(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setattr("agentevals.openai_eval_backend._get_openai_client", lambda: None)
        d = OpenAIEvalDef(name="sc", grader=_score_grader())
        result = await evaluate_openai_eval(d, [_invocation("hi")], None)
        assert "expected invocations" not in (result.error or "")
