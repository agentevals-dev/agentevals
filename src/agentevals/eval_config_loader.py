"""Load evaluation configuration from a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import (
    BuiltinMetricDef,
    CodeEvaluatorDef,
    EvalRunConfig,
    EvaluatorDef,
    OpenAIEvalDef,
    RemoteEvaluatorDef,
)

_TYPE_TO_MODEL = {
    "builtin": BuiltinMetricDef,
    "code": CodeEvaluatorDef,
    "remote": RemoteEvaluatorDef,
    "openai_eval": OpenAIEvalDef,
}


def _parse_evaluator_entry(entry: dict[str, Any]) -> EvaluatorDef:
    """Parse a single evaluator entry from the YAML config."""
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
    return model_cls.model_validate(entry)


def load_eval_config(path: str | Path) -> EvalRunConfig:
    """Load an eval config YAML file and return a partially-filled EvalRunConfig.

    The YAML file uses an ``evaluators`` list where each entry is a dict with
    ``name`` and ``type`` fields.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Eval config file not found: {path}")

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Eval config must be a YAML mapping, got {type(data).__name__}")

    legacy_keys = {
        "metrics",
        "custom_evaluators",
        "judge_model",
        "threshold",
        "trajectory_match_type",
    }
    present_legacy_keys = sorted(key for key in legacy_keys if key in data)
    if present_legacy_keys:
        raise ValueError(
            "Legacy eval config keys are no longer supported: "
            + ", ".join(present_legacy_keys)
            + ". Use 'evaluators' instead."
        )

    raw_evaluators = data.get("evaluators", [])
    if not isinstance(raw_evaluators, list):
        raise ValueError("'evaluators' must be a list")

    evaluator_defs = [_parse_evaluator_entry(entry) for entry in raw_evaluators]

    config = EvalRunConfig(
        trace_files=[],
        evaluators=evaluator_defs,
    )

    if "eval_set" in data:
        config.eval_set_file = str(data["eval_set"])
    if "trace_format" in data:
        config.trace_format = data["trace_format"]
    if "output_format" in data:
        config.output_format = str(data["output_format"])
    elif "output" in data:
        config.output_format = str(data["output"])

    return config


def merge_configs(file_config: EvalRunConfig, cli_config: EvalRunConfig) -> EvalRunConfig:
    """Merge a file-based config with CLI overrides.

    CLI values take precedence for scalar fields. Evaluator definitions are
    merged by evaluator name, with CLI-provided entries replacing file entries
    of the same identity.
    """
    merged = file_config.model_copy(deep=True)

    if cli_config.trace_files:
        merged.trace_files = cli_config.trace_files
    if cli_config.eval_set_file is not None:
        merged.eval_set_file = cli_config.eval_set_file
    if cli_config.trace_format is not None:
        merged.trace_format = cli_config.trace_format
    if cli_config.output_format != "table":
        merged.output_format = cli_config.output_format

    merged_evaluators: dict[str, EvaluatorDef] = {e.name: e for e in merged.evaluators}
    for evaluator in cli_config.evaluators:
        merged_evaluators[evaluator.name] = evaluator
    merged.evaluators = list(merged_evaluators.values())

    return merged
