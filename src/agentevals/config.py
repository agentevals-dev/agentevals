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
        if grader_type == "text_similarity":
            metric = v.get("evaluation_metric")
            if not metric:
                raise ValueError("'evaluation_metric' is required for text_similarity grader")
            if metric not in _VALID_SIMILARITY_METRICS:
                raise ValueError(f"Unknown evaluation_metric '{metric}'. Valid: {sorted(_VALID_SIMILARITY_METRICS)}")
        elif grader_type == "string_check":
            operation = v.get("operation")
            if not operation:
                raise ValueError("'operation' is required for string_check grader")
            if operation not in _VALID_STRING_CHECK_OPERATIONS:
                raise ValueError(f"Unknown operation '{operation}'. Valid: {sorted(_VALID_STRING_CHECK_OPERATIONS)}")
            if "reference" not in v:
                raise ValueError("'reference' is required for string_check grader")
        else:
            supported = "'text_similarity', 'string_check'"
            raise ValueError(f"Unsupported grader type '{grader_type}'. Supported: {supported}")
        return v


CustomEvaluatorDef = Annotated[
    BuiltinMetricDef | CodeEvaluatorDef | RemoteEvaluatorDef | OpenAIEvalDef,
    Field(discriminator="type"),
]


class EvalRunConfig(BaseModel):
    trace_files: list[str] = Field(description="Paths to trace files (Jaeger JSON or OTLP JSON).")

    eval_set_file: str | None = Field(
        default=None,
        description="Path to a golden eval set JSON file (ADK EvalSet format).",
    )

    metrics: list[str] = Field(
        default_factory=lambda: ["tool_trajectory_avg_score"],
        description="List of built-in metric names to evaluate.",
    )

    custom_evaluators: list[CustomEvaluatorDef] = Field(
        default_factory=list,
        description="Custom evaluator definitions.",
    )

    trace_format: str = Field(
        default="jaeger-json",
        description="Format of the trace files (jaeger-json or otlp-json).",
    )

    judge_model: str | None = Field(
        default=None,
        description="LLM model for judge-based metrics.",
    )

    threshold: float | None = Field(
        default=None,
        description="Score threshold for pass/fail.",
    )

    trajectory_match_type: str | None = Field(
        default=None,
        description="Match type for tool_trajectory_avg_score: 'EXACT', 'IN_ORDER', or 'ANY_ORDER'. Default: EXACT.",
    )

    @field_validator("trajectory_match_type")
    @classmethod
    def _validate_trajectory_match_type(cls, v: str | None) -> str | None:
        valid = {"EXACT", "IN_ORDER", "ANY_ORDER"}
        if v is not None and v.upper() not in valid:
            raise ValueError(f"Invalid trajectory_match_type '{v}'. Valid values: {sorted(valid)}")
        return v.upper() if v is not None else v

    output_format: str = Field(
        default="table",
        description="Output format: 'table', 'json', or 'summary'.",
    )

    max_concurrent_traces: int = Field(
        default=10,
        description="Maximum number of traces to evaluate concurrently.",
    )

    max_concurrent_evals: int = Field(
        default=5,
        description="Maximum number of concurrent metric evaluations (LLM API calls).",
    )
