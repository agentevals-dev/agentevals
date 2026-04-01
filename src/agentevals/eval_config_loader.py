"""Load evaluation configuration from a YAML file."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .config import (
    BuiltinMetricDef,
    CodeEvaluatorDef,
    CustomEvaluatorDef,
    EvalRunConfig,
    OpenAIEvalDef,
    RemoteEvaluatorDef,
)

logger = logging.getLogger(__name__)

_TYPE_TO_MODEL = {
    "builtin": BuiltinMetricDef,
    "code": CodeEvaluatorDef,
    "remote": RemoteEvaluatorDef,
    "openai_eval": OpenAIEvalDef,
}


def _parse_evaluator_entry(entry: dict[str, Any]) -> tuple[str | None, CustomEvaluatorDef | None]:
    """Parse a single evaluator entry from the YAML config.

    Every entry must be a dict with ``name`` and ``type`` fields.
    Returns (builtin_name, custom_evaluator_def).  Exactly one will be non-None.
    """
    if not isinstance(entry, dict):
        raise ValueError(
            f"Each evaluator entry must be a mapping with 'name' and 'type' fields, got {type(entry).__name__}: {entry!r}"
        )

    name = entry.get("name")
    if not name:
        raise ValueError(f"Evaluator entry must have a 'name' field: {entry}")

    evaluator_type = entry.get("type")
    if not evaluator_type:
        raise ValueError(f"Evaluator entry '{name}' must have a 'type' field ({', '.join(_TYPE_TO_MODEL)})")

    if evaluator_type not in _TYPE_TO_MODEL:
        raise ValueError(
            f"Unknown evaluator type '{evaluator_type}' for '{name}'. Valid types: {list(_TYPE_TO_MODEL.keys())}"
        )

    model_cls = _TYPE_TO_MODEL[evaluator_type]
    evaluator_def = model_cls.model_validate(entry)

    if evaluator_type == "builtin":
        return name, evaluator_def if (
            evaluator_def.threshold is not None or evaluator_def.judge_model is not None
        ) else None

    return None, evaluator_def


def load_eval_config(path: str | Path) -> EvalRunConfig:
    """Load an eval config YAML file and return a partially-filled EvalRunConfig.

    The YAML file uses an ``evaluators`` list where each entry is a dict with
    ``name`` and ``type`` fields.  Built-in entries populate ``metrics``;
    code/remote entries populate ``custom_evaluators``.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Eval config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Eval config must be a YAML mapping, got {type(data).__name__}")

    raw_evaluators = data.get("evaluators", [])
    if not isinstance(raw_evaluators, list):
        raise ValueError("'evaluators' must be a list")

    builtin_names: list[str] = []
    custom_defs: list[CustomEvaluatorDef] = []
    builtin_overrides: dict[str, BuiltinMetricDef] = {}

    for entry in raw_evaluators:
        builtin_name, custom_def = _parse_evaluator_entry(entry)
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
        custom_evaluators=custom_defs,
    )

    if "eval_set" in data:
        config.eval_set_file = str(data["eval_set"])
    if "judge_model" in data:
        config.judge_model = data["judge_model"]
    if "threshold" in data:
        config.threshold = float(data["threshold"])
    if "trajectory_match_type" in data:
        config.trajectory_match_type = data["trajectory_match_type"]
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
    if cli_config.trajectory_match_type is not None:
        merged.trajectory_match_type = cli_config.trajectory_match_type
    if cli_config.trace_format != "jaeger-json":
        merged.trace_format = cli_config.trace_format
    if cli_config.output_format != "table":
        merged.output_format = cli_config.output_format

    file_metric_names = set(merged.metrics)
    for name in cli_config.metrics:
        if name not in file_metric_names:
            merged.metrics.append(name)

    return merged
