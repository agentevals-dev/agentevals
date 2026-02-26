"""Configuration for agentevals runs."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EvalRunConfig(BaseModel):
    trace_files: list[str] = Field(description="Paths to trace files (Jaeger JSON or OTLP JSON).")

    eval_set_file: Optional[str] = Field(
        default=None,
        description="Path to a golden eval set JSON file (ADK EvalSet format).",
    )

    metrics: list[str] = Field(
        default_factory=lambda: ["tool_trajectory_avg_score"],
        description="List of metric names to evaluate.",
    )

    trace_format: str = Field(
        default="jaeger-json",
        description="Format of the trace files (jaeger-json or otlp-json).",
    )

    judge_model: Optional[str] = Field(
        default=None,
        description="LLM model for judge-based metrics.",
    )

    threshold: Optional[float] = Field(
        default=None,
        description="Score threshold for pass/fail.",
    )

    output_format: str = Field(
        default="table",
        description="Output format: 'table', 'json', or 'summary'.",
    )
