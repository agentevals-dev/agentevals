"""Load evaluation configuration from a YAML file."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .config import (
    BuiltinMetricDef,
    CodeGraderDef,
    CustomGraderDef,
    EvalRunConfig,
)

logger = logging.getLogger(__name__)

_TYPE_TO_MODEL = {
    "builtin": BuiltinMetricDef,
    "code": CodeGraderDef,
}


def _parse_grader_entry(entry: str | dict[str, Any]) -> tuple[str | None, CustomGraderDef | None]:
    """Parse a single grader entry from the YAML config.

    Returns (builtin_name, custom_grader_def).  Exactly one will be non-None.
    Plain strings and dicts without a ``type`` key (or type=builtin) that have
    no extra fields beyond name/threshold/judge_model are treated as built-in
    metric name references so they flow through the existing ``metrics`` list.
    """
    if isinstance(entry, str):
        return entry, None

    if not isinstance(entry, dict):
        raise ValueError(f"Grader entry must be a string or dict, got {type(entry).__name__}")

    name = entry.get("name")
    if not name:
        raise ValueError(f"Grader entry dict must have a 'name' field: {entry}")

    grader_type = entry.get("type", "builtin")

    if grader_type not in _TYPE_TO_MODEL:
        raise ValueError(
            f"Unknown grader type '{grader_type}' for '{name}'. "
            f"Valid types: {list(_TYPE_TO_MODEL.keys())}"
        )

    model_cls = _TYPE_TO_MODEL[grader_type]
    grader_def = model_cls.model_validate(entry)

    if grader_type == "builtin":
        return name, grader_def if (grader_def.threshold is not None or grader_def.judge_model is not None) else None

    return None, grader_def


def load_eval_config(path: str | Path) -> EvalRunConfig:
    """Load an eval config YAML file and return a partially-filled EvalRunConfig.

    The returned config will have ``metrics`` (built-in names) and
    ``custom_graders`` populated.  Callers should merge these with any
    CLI/API overrides and fill in ``trace_files`` etc.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Eval config file not found: {path}")

    with open(path, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Eval config must be a YAML mapping, got {type(data).__name__}")

    raw_metrics = data.get("metrics", [])
    if not isinstance(raw_metrics, list):
        raise ValueError("'metrics' must be a list")

    builtin_names: list[str] = []
    custom_defs: list[CustomGraderDef] = []
    builtin_overrides: dict[str, BuiltinMetricDef] = {}

    for entry in raw_metrics:
        builtin_name, custom_def = _parse_grader_entry(entry)
        if builtin_name:
            builtin_names.append(builtin_name)
        if custom_def:
            if isinstance(custom_def, BuiltinMetricDef):
                builtin_overrides[custom_def.name] = custom_def
                if custom_def.name not in builtin_names:
                    builtin_names.append(custom_def.name)
            else:
                custom_defs.append(custom_def)

    config = EvalRunConfig(
        trace_files=[],
        metrics=builtin_names,
        custom_graders=custom_defs,
    )

    if "eval_set" in data:
        config.eval_set_file = str(data["eval_set"])
    if "judge_model" in data:
        config.judge_model = data["judge_model"]
    if "threshold" in data:
        config.threshold = float(data["threshold"])
    if "trace_format" in data:
        config.trace_format = data["trace_format"]

    config._builtin_overrides = builtin_overrides  # type: ignore[attr-defined]

    return config


def merge_configs(file_config: EvalRunConfig, cli_config: EvalRunConfig) -> EvalRunConfig:
    """Merge a file-based config with CLI overrides.

    CLI values take precedence for scalar fields.  Metrics lists are merged:
    CLI ``--metric`` flags are added to the file config's built-in metrics
    (duplicates removed).
    """
    merged = file_config.model_copy()

    if cli_config.trace_files:
        merged.trace_files = cli_config.trace_files
    if cli_config.eval_set_file is not None:
        merged.eval_set_file = cli_config.eval_set_file
    if cli_config.judge_model is not None:
        merged.judge_model = cli_config.judge_model
    if cli_config.threshold is not None:
        merged.threshold = cli_config.threshold
    if cli_config.trace_format != "jaeger-json":
        merged.trace_format = cli_config.trace_format
    if cli_config.output_format != "table":
        merged.output_format = cli_config.output_format

    cli_metric_names = set(cli_config.metrics)
    file_metric_names = set(merged.metrics)
    for name in cli_config.metrics:
        if name not in file_metric_names:
            merged.metrics.append(name)

    return merged
