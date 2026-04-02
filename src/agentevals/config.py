"""Configuration for agentevals runs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator


class BuiltinMetricDef(BaseModel):
    """A built-in ADK metric, optionally with threshold/judge overrides."""

    name: str
    type: Literal["builtin"] = "builtin"
    threshold: float | None = None
    judge_model: str | None = None


class BaseEvaluatorDef(BaseModel):
    """Shared fields for all executable evaluator definitions."""

    name: str
    threshold: float = 0.5
    timeout: int = Field(default=30, description="Subprocess timeout in seconds.")
    config: dict[str, Any] = Field(default_factory=dict)
    executor: str = Field(default="local", description="Execution environment: 'local' or 'docker' (future).")


class CodeEvaluatorDef(BaseEvaluatorDef):
    """An evaluator implemented as an external code file (Python, JavaScript, etc.)."""

    type: Literal["code"] = "code"
    path: str = Field(description="Path to the evaluator file (.py, .js, .ts, etc.).")

    @field_validator("path")
    @classmethod
    def _validate_extension(cls, v: str) -> str:
        from .custom_evaluators import supported_extensions

        suffix = Path(v).suffix.lower()
        allowed = supported_extensions()
        if suffix not in allowed:
            raise ValueError(f"Unsupported evaluator file extension '{suffix}'. Supported: {sorted(allowed)}")
        return v


class RemoteEvaluatorDef(BaseEvaluatorDef):
    """An evaluator fetched from a remote source (GitHub, registry, etc.)."""

    type: Literal["remote"] = "remote"
    source: str = Field(default="github", description="Evaluator source (e.g. 'github').")
    ref: str = Field(description="Source-specific reference (e.g. path within the repo).")


_VALID_SIMILARITY_METRICS = frozenset(
    {
        "fuzzy_match",
        "bleu",
        "gleu",
        "meteor",
        "cosine",
        "rouge_1",
        "rouge_2",
        "rouge_3",
        "rouge_4",
        "rouge_5",
        "rouge_l",
    }
)

_VALID_STRING_CHECK_OPERATIONS = frozenset(
    {
        "eq",
        "ne",
        "like",
        "ilike",
        "contains",
        "not_contains",
        "starts_with",
        "ends_with",
    }
)

# All supported grader types — use this constant in error messages and checks.
_SUPPORTED_GRADER_TYPES = frozenset({"text_similarity", "string_check"})


class OpenAIEvalDef(BaseModel):
    """An evaluator that delegates grading to the OpenAI Evals API."""

    type: Literal["openai_eval"] = "openai_eval"
    name: str
    threshold: float = 0.5
    timeout: int = Field(default=120, description="Max seconds to wait for the OpenAI eval run to complete.")
    grader: dict[str, Any] = Field(description="OpenAI grader config passed to testing_criteria.")

    @field_validator("grader")
    @classmethod
    def _validate_grader(cls, v: dict[str, Any]) -> dict[str, Any]:
        grader_type = v.get("type")
        if grader_type not in _SUPPORTED_GRADER_TYPES:
            raise ValueError(
                f"Unsupported grader type '{grader_type}'. "
                f"Supported: {sorted(_SUPPORTED_GRADER_TYPES)}"
            )

        if grader_type == "text_similarity":
            metric = v.get("evaluation_metric")
            if not metric:
                raise ValueError("'evaluation_metric' is required for text_similarity grader")
            if metric not in _VALID_SIMILARITY_METRICS:
                raise ValueError(
                    f"Unknown evaluation_metric '{metric}'. Valid: {sorted(_VALID_SIMILARITY_METRICS)}"
                )
        elif grader_type == "string_check":
            operation = v.get("operation")
            if not operation:
                raise ValueError("'operation' is required for string_check grader")
            if operation not in _VALID_STRING_CHECK_OPERATIONS:
                raise ValueError(
                    f"Unknown operation '{operation}'. Valid: {sorted(_VALID_STRING_CHECK_OPERATIONS)}"
                )
            if "reference" not in v:
                raise ValueError("'reference' is required for string_check grader")

        return v


CustomEvaluatorDef = Annotated[
    BuiltinMetricDef | CodeEvaluatorDef | RemoteEvaluatorDef | OpenAIEvalDef,
    Field(discriminator="type"),
]
