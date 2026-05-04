"""Unit tests for the OpenAI Evals backend — covers both text_similarity and string_check graders."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from agentevals.config import OpenAIEvalDef
from agentevals.openai_eval_backend import (
    _build_jsonl_items,
    _build_testing_criteria,
    _extract_item_score,
    _get_item_schema,
    evaluate_openai_eval,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_invocation(text: str):
    """Build a minimal Invocation-like object with a final_response."""
    inv = MagicMock()
    inv.final_response = text
    return inv


def _make_text_similarity_def(**overrides) -> OpenAIEvalDef:
    defaults = {
        "name": "test_similarity",
        "threshold": 0.7,
        "grader": {
            "type": "text_similarity",
            "evaluation_metric": "fuzzy_match",
        },
    }
    defaults.update(overrides)
    return OpenAIEvalDef(**defaults)


def _make_string_check_def(**overrides) -> OpenAIEvalDef:
    defaults = {
        "name": "test_check",
        "grader": {
            "type": "string_check",
            "operation": "eq",
            "reference": "Paris",
        },
    }
    defaults.update(overrides)
    return OpenAIEvalDef(**defaults)


# ── Config validation tests ────────────────────────────────────────────────────


class TestOpenAIEvalDefValidation:
    """Verify that the pydantic validator enforces correct grader configs."""

    def test_text_similarity_requires_evaluation_metric(self):
        with pytest.raises(ValidationError, match="evaluation_metric"):
            OpenAIEvalDef(name="x", grader={"type": "text_similarity"})

    def test_text_similarity_rejects_invalid_metric(self):
        with pytest.raises(ValidationError, match="Unknown evaluation_metric"):
            OpenAIEvalDef(
                name="x",
                grader={"type": "text_similarity", "evaluation_metric": "bogus"},
            )

    def test_text_similarity_accepts_valid_metrics(self):
        for metric in ("fuzzy_match", "bleu", "cosine", "rouge_l"):
            d = OpenAIEvalDef(
                name="x",
                grader={"type": "text_similarity", "evaluation_metric": metric},
            )
            assert d.grader["evaluation_metric"] == metric

    def test_string_check_requires_operation(self):
        with pytest.raises(ValidationError, match="operation"):
            OpenAIEvalDef(name="x", grader={"type": "string_check", "reference": "hi"})

    def test_string_check_requires_reference(self):
        with pytest.raises(ValidationError, match="reference"):
            OpenAIEvalDef(name="x", grader={"type": "string_check", "operation": "eq"})

    def test_string_check_rejects_invalid_operation(self):
        with pytest.raises(ValidationError, match="Unknown operation"):
            OpenAIEvalDef(
                name="x",
                grader={"type": "string_check", "operation": "contains", "reference": "hi"},
            )

    def test_string_check_accepts_valid_operations(self):
        for op in ("eq", "ne", "like", "ilike"):
            d = OpenAIEvalDef(
                name="x",
                grader={"type": "string_check", "operation": op, "reference": "val"},
            )
            assert d.grader["operation"] == op

    def test_unsupported_grader_type_raises(self):
        with pytest.raises(ValidationError, match="Unsupported grader type"):
            OpenAIEvalDef(name="x", grader={"type": "model_graded"})


# ── Item schema tests ──────────────────────────────────────────────────────────


class TestGetItemSchema:
    def test_string_check_schema_has_actual_only(self):
        schema = _get_item_schema("string_check")
        assert schema["required"] == ["actual_response"]
        assert "expected_response" not in schema["properties"]

    def test_text_similarity_schema_has_both(self):
        schema = _get_item_schema("text_similarity")
        assert "actual_response" in schema["required"]
        assert "expected_response" in schema["required"]


# ── Testing criteria tests ─────────────────────────────────────────────────────


class TestBuildTestingCriteria:
    def test_text_similarity_criteria(self):
        ev = _make_text_similarity_def(threshold=0.8)
        criteria = _build_testing_criteria(ev)
        assert criteria["type"] == "text_similarity"
        assert criteria["evaluation_metric"] == "fuzzy_match"
        assert criteria["pass_threshold"] == 0.8
        assert criteria["reference"] == "{{ item.expected_response }}"

    def test_string_check_criteria(self):
        ev = _make_string_check_def()
        criteria = _build_testing_criteria(ev)
        assert criteria["type"] == "string_check"
        assert criteria["operation"] == "eq"
        assert criteria["reference"] == "Paris"
        assert criteria["input"] == "{{ item.actual_response }}"
        assert "pass_threshold" not in criteria

    def test_unsupported_grader_raises(self):
        ev = _make_text_similarity_def()
        # Bypass pydantic validation to test the function directly
        ev.grader = {"type": "unknown"}
        with pytest.raises(ValueError, match="Unsupported grader type"):
            _build_testing_criteria(ev)


# ── JSONL item building tests ──────────────────────────────────────────────────


class TestBuildJsonlItems:
    def test_text_similarity_includes_expected(self):
        actual = [_make_invocation("hello")]
        expected = [_make_invocation("hi")]

        with patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x):
            items = _build_jsonl_items(actual, expected, grader_type="text_similarity")

        assert len(items) == 1
        assert items[0]["item"]["actual_response"] == "hello"
        assert items[0]["item"]["expected_response"] == "hi"

    def test_string_check_excludes_expected(self):
        actual = [_make_invocation("Paris")]
        expected = [_make_invocation("ignored")]

        with patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x):
            items = _build_jsonl_items(actual, expected, grader_type="string_check")

        assert len(items) == 1
        assert items[0]["item"]["actual_response"] == "Paris"
        assert "expected_response" not in items[0]["item"]

    def test_missing_expected_uses_empty_string(self):
        actual = [_make_invocation("a"), _make_invocation("b")]
        expected = [_make_invocation("x")]

        with patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x):
            items = _build_jsonl_items(actual, expected, grader_type="text_similarity")

        assert items[1]["item"]["expected_response"] == ""

    def test_empty_invocations_returns_empty(self):
        with patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x):
            items = _build_jsonl_items([], [], grader_type="string_check")
        assert items == []


# ── Item score extraction ──────────────────────────────────────────────────────


class TestExtractItemScore:
    def test_returns_score(self):
        item = MagicMock()
        result = MagicMock()
        result.score = 0.85
        item.results = [result]
        assert _extract_item_score(item) == 0.85

    def test_returns_none_when_no_results(self):
        item = MagicMock()
        item.results = []
        assert _extract_item_score(item) is None

    def test_returns_none_when_results_attr_missing(self):
        item = MagicMock(spec=[])  # no attributes
        assert _extract_item_score(item) is None


# ── Integration tests (mocked OpenAI client) ───────────────────────────────────


class TestEvaluateOpenAIEval:
    def _make_mock_client(self, run_status="completed", scores=None):
        """Create a fully mocked OpenAI client."""
        client = MagicMock()

        # evals.create
        eval_obj = MagicMock()
        eval_obj.id = "eval_123"
        client.evals.create.return_value = eval_obj

        # evals.runs.create
        run_obj = MagicMock()
        run_obj.id = "run_456"
        client.evals.runs.create.return_value = run_obj

        # evals.runs.retrieve
        completed_run = MagicMock()
        completed_run.status = run_status
        completed_run.result_counts = MagicMock()
        completed_run.result_counts.passed = len(scores or [])
        completed_run.result_counts.failed = 0
        completed_run.result_counts.total = len(scores or [])
        completed_run.per_testing_criteria_results = None
        client.evals.runs.retrieve.return_value = completed_run

        # evals.runs.output_items.list
        output_items = []
        for s in scores or []:
            item = MagicMock()
            result = MagicMock()
            result.score = s
            item.results = [result]
            output_items.append(item)
        page = MagicMock()
        page.data = output_items
        client.evals.runs.output_items.list.return_value = page

        # evals.delete
        client.evals.delete.return_value = None

        return client

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x)
    @patch("agentevals.openai_eval_backend._get_openai_client")
    def test_string_check_success(self, mock_get_client, mock_content):
        client = self._make_mock_client(scores=[1.0])
        mock_get_client.return_value = client

        ev = _make_string_check_def()
        actual = [_make_invocation("Paris")]

        result = asyncio.run(evaluate_openai_eval(ev, actual, None))

        assert result.error is None
        assert result.score == 1.0
        assert result.eval_status == "PASSED"
        assert result.details["operation"] == "eq"

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x)
    @patch("agentevals.openai_eval_backend._get_openai_client")
    def test_text_similarity_requires_expected(self, mock_get_client, mock_content):
        ev = _make_text_similarity_def()
        actual = [_make_invocation("hello")]

        result = asyncio.run(evaluate_openai_eval(ev, actual, None))

        assert result.error is not None
        assert "expected invocations" in result.error

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x)
    @patch("agentevals.openai_eval_backend._get_openai_client")
    def test_text_similarity_success(self, mock_get_client, mock_content):
        client = self._make_mock_client(scores=[0.9, 0.8])
        mock_get_client.return_value = client

        ev = _make_text_similarity_def()
        actual = [_make_invocation("hello"), _make_invocation("world")]
        expected = [_make_invocation("hi"), _make_invocation("earth")]

        result = asyncio.run(evaluate_openai_eval(ev, actual, expected))

        assert result.error is None
        assert result.score == pytest.approx(0.85)
        assert result.details["evaluation_metric"] == "fuzzy_match"

    @patch.dict("os.environ", {"OPENAI_API_KEY": ""})
    def test_missing_api_key_returns_error(self):
        ev = _make_string_check_def()
        actual = [_make_invocation("Paris")]

        result = asyncio.run(evaluate_openai_eval(ev, actual, None))

        assert result.error is not None
        assert "OPENAI_API_KEY" in result.error

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x)
    @patch("agentevals.openai_eval_backend._get_openai_client")
    def test_string_check_no_expected_needed(self, mock_get_client, mock_content):
        """string_check grader should work without expected_invocations (None)."""
        client = self._make_mock_client(scores=[1.0])
        mock_get_client.return_value = client

        ev = _make_string_check_def()
        actual = [_make_invocation("Paris")]

        result = asyncio.run(evaluate_openai_eval(ev, actual, None))

        # Verify it didn't short-circuit with an error
        assert result.error is None
        assert result.eval_status == "PASSED"

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    @patch("agentevals.openai_eval_backend._content_to_text", side_effect=lambda x: x)
    @patch("agentevals.openai_eval_backend._get_openai_client")
    def test_empty_invocations_returns_error(self, mock_get_client, mock_content):
        ev = _make_string_check_def()

        result = asyncio.run(evaluate_openai_eval(ev, [], None))

        assert result.error is not None
        assert "No invocations" in result.error
