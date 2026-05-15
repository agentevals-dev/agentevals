"""Configuration for agentevals runs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


def _normalize_trajectory_match_type(v: str | None) -> str | None:
    valid = {"EXACT", "IN_ORDER", "ANY_ORDER"}
    if v is not None and v.upper() not in valid:
        raise ValueError(f"Invalid trajectory_match_type '{v}'. Valid values: {sorted(valid)}")
    return v.upper() if v is not None else v


class BuiltinMetricDef(BaseModel):
    """A built-in ADK metric, optionally with threshold/judge overrides."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    type: Literal["builtin"] = "builtin"
    threshold: float | None = Field(default=None, ge=0, le=1)
    judge_model: str | None = None
    trajectory_match_type: str | None = None

    @field_validator("trajectory_match_type")
    @classmethod
    def _validate_trajectory_match_type(cls, v: str | None) -> str | None:
        return _normalize_trajectory_match_type(v)


class BaseEvaluatorDef(BaseModel):
    """Shared fields for all executable evaluator definitions."""

    name: str
    threshold: float = Field(default=0.5, ge=0, le=1)
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


class OpenAIEvalDef(BaseModel):
    """An evaluator that delegates grading to the OpenAI Evals API."""

    type: Literal["openai_eval"] = "openai_eval"
    name: str
    threshold: float = Field(default=0.5, ge=0, le=1)
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
        elif grader_type == "label_model":
            for field in ("model", "input", "labels", "passing_labels"):
                if not v.get(field):
                    raise ValueError(f"'{field}' is required for label_model grader")
            invalid = [lbl for lbl in v["passing_labels"] if lbl not in v["labels"]]
            if invalid:
                raise ValueError(f"passing_labels contains labels not declared in labels: {invalid}")
        else:
            raise ValueError(f"Unsupported grader type: '{grader_type}'. Supported: label_model, text_similarity")
        return v


EvaluatorDef = Annotated[
    BuiltinMetricDef | CodeEvaluatorDef | RemoteEvaluatorDef | OpenAIEvalDef,
    Field(discriminator="type"),
]


def make_builtin_evaluator_entries(
    metric_names: list[str] | None,
    *,
    judge_model: str | None = None,
    threshold: float | None = None,
    trajectory_match_type: str | None = None,
) -> list[BuiltinMetricDef]:
    metrics = metric_names if metric_names is not None else ["tool_trajectory_avg_score"]

    evaluators: list[BuiltinMetricDef] = []
    for name in metrics:
        evaluators.append(
            BuiltinMetricDef(
                name=name,
                judge_model=judge_model,
                threshold=threshold,
                trajectory_match_type=trajectory_match_type,
            )
        )
    return evaluators


def apply_builtin_overrides(
    evaluators: list[EvaluatorDef],
    *,
    judge_model: str | None = None,
    threshold: float | None = None,
    trajectory_match_type: str | None = None,
) -> list[EvaluatorDef]:
    """Return a new evaluator list with run-level overrides applied to built-ins.

    Non-builtin entries pass through unchanged. Each override is only applied
    when the corresponding argument is not None, so callers can pass any subset.
    """
    updated: list[EvaluatorDef] = []
    for evaluator in evaluators:
        if isinstance(evaluator, BuiltinMetricDef):
            payload = evaluator.model_dump(by_alias=False)
            if judge_model is not None:
                payload["judge_model"] = judge_model
            if threshold is not None:
                payload["threshold"] = threshold
            if trajectory_match_type is not None:
                payload["trajectory_match_type"] = trajectory_match_type
            updated.append(BuiltinMetricDef.model_validate(payload))
        else:
            updated.append(evaluator)
    return updated


class EvalParams(BaseModel):
    """Evaluation parameters independent of how traces are provided.

    Used by ``run_evaluation_from_traces`` for programmatic / API-driven
    evaluation.  ``EvalRunConfig`` inherits from this and adds file I/O fields.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    evaluators: list[EvaluatorDef] = Field(
        default_factory=lambda: [BuiltinMetricDef(name="tool_trajectory_avg_score")],
        description="Evaluator definitions, including built-in evaluators and custom evaluators.",
    )

    @field_validator("evaluators")
    @classmethod
    def _validate_evaluators(cls, v: list[EvaluatorDef]) -> list[EvaluatorDef]:
        if not v:
            raise ValueError("At least one evaluator is required.")
        duplicate_names = sorted(
            name for name, count in Counter(evaluator.name for evaluator in v).items() if count > 1
        )
        if duplicate_names:
            raise ValueError("Evaluator names must be globally unique. Duplicate names: " + ", ".join(duplicate_names))
        return v

    max_concurrent_traces: int = Field(
        default=10,
        ge=1,
        description="Maximum number of traces to evaluate concurrently.",
    )

    max_concurrent_evals: int = Field(
        default=5,
        ge=1,
        description="Maximum number of concurrent evaluator executions (for example LLM API calls).",
    )


class EvalRunConfig(EvalParams):
    """Full configuration for file-based evaluation runs."""

    trace_files: list[str] = Field(description="Paths to trace files (Jaeger or OTLP JSON, .json or .jsonl).")

    eval_set_file: str | None = Field(
        default=None,
        description="Path to a golden eval set JSON file (ADK EvalSet format).",
    )

    trace_format: str | None = Field(
        default=None,
        description=(
            "Optional explicit trace format override ('jaeger-json' or 'otlp-json'). "
            "Leave unset to auto-detect from file contents."
        ),
    )

    output_format: str = Field(
        default="table",
        description="Output format: 'table', 'json', or 'summary'.",
    )
