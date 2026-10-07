"""Configuration for agentevals runs."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


def _normalize_trajectory_match_type(v: str | None) -> str | None:
    valid = {"EXACT", "IN_ORDER", "ANY_ORDER"}
    if v is not None and v.upper() not in valid:
        raise ValueError(f"Invalid trajectory_match_type '{v}'. Valid values: {sorted(valid)}")
    return v.upper() if v is not None else v


UNSUPPORTED_BUILTIN_METRICS = {
    "per_turn_user_simulator_quality_v1": "it scores a simulated user, and agentevals evaluates recorded traces",
}

GroupBy = Literal["auto", "trace", "conversation"]


class BuiltinMetricDef(BaseModel):
    """A built-in ADK metric, optionally with threshold/judge overrides."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    type: Literal["builtin"] = "builtin"
    threshold: float | None = Field(default=None, ge=0, le=1)
    judge_model: str | None = None
    trajectory_match_type: str | None = None
    credential_ref: str | None = Field(
        default=None,
        description="Logical name of a RunSpec.credential_refs entry whose resolved value is the judge API key.",
    )
    judge_base_url: str | None = Field(
        default=None,
        description="Optional base URL for the judge endpoint (e.g. an OpenAI-compatible proxy).",
    )

    @field_validator("trajectory_match_type")
    @classmethod
    def _validate_trajectory_match_type(cls, v: str | None) -> str | None:
        return _normalize_trajectory_match_type(v)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if v in UNSUPPORTED_BUILTIN_METRICS:
            raise ValueError(f"Built-in metric '{v}' is not supported: {UNSUPPORTED_BUILTIN_METRICS[v]}")
        return v


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

    @field_validator("ref")
    @classmethod
    def _validate_ref(cls, v: str) -> str:
        return validate_remote_ref(v)

    @model_validator(mode="after")
    def _validate_source(self) -> RemoteEvaluatorDef:
        if self.source in _NON_FETCHABLE_SOURCES:
            raise ValueError(f"Evaluator source '{self.source}' cannot be fetched; use a builtin evaluator instead")
        return self


_NON_FETCHABLE_SOURCES = frozenset({"builtin"})
_REF_MAX_LENGTH = 512
_REF_MAX_SEGMENTS = 16
_REF_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")


def validate_remote_ref(ref: str) -> str:
    """Accept only a plain relative POSIX path to a supported evaluator file.

    The ref is joined into a local cache path and a download URL, so anything
    that could change either base (absolute paths, dot segments, backslashes,
    percent escapes) is rejected rather than normalized.
    """
    from .custom_evaluators import supported_extensions

    if not ref or len(ref) > _REF_MAX_LENGTH:
        raise ValueError(f"Remote evaluator ref must be 1 to {_REF_MAX_LENGTH} characters")
    if "\\" in ref or "%" in ref:
        raise ValueError("Remote evaluator ref must not contain backslashes or percent escapes")
    segments = ref.split("/")
    if len(segments) > _REF_MAX_SEGMENTS:
        raise ValueError(f"Remote evaluator ref must have at most {_REF_MAX_SEGMENTS} path segments")
    if not all(_REF_SEGMENT.fullmatch(seg) for seg in segments):
        raise ValueError(
            "Remote evaluator ref must be a relative path whose segments use only letters, digits, "
            "'_', '-' and single dots between name parts"
        )
    suffix = PurePosixPath(ref).suffix.lower()
    allowed = supported_extensions()
    if suffix not in allowed:
        raise ValueError(f"Unsupported evaluator file extension '{suffix}'. Supported: {sorted(allowed)}")
    return ref


EvaluatorDef = Annotated[
    BuiltinMetricDef | CodeEvaluatorDef | RemoteEvaluatorDef,
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

    group_by: GroupBy = Field(
        default="auto",
        description=(
            "How traces form evaluation units. 'trace': every trace on its own. 'conversation': traces "
            "sharing agentevals.session_name, gen_ai.conversation.id or session.id are evaluated together. "
            "'auto': conversation when any trace carries agentevals.session_name or the eval set has a "
            "multi turn case, otherwise trace."
        ),
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
