from __future__ import annotations

import textwrap

import pytest

from agentevals.config import BuiltinMetricDef, CodeEvaluatorDef, EvalParams, EvalRunConfig
from agentevals.eval_config_loader import load_eval_config, merge_configs


def test_load_eval_config_rejects_legacy_keys(tmp_path):
    config_file = tmp_path / "legacy_eval.yaml"
    config_file.write_text(
        textwrap.dedent(
            """
            metrics:
              - tool_trajectory_avg_score
            custom_evaluators:
              - name: tool_call_checker
                type: code
                path: ./examples/custom_evaluators/tool_call_checker.py
            judge_model: gemini-2.5-flash
            threshold: 0.8
            trajectory_match_type: EXACT
            eval_set: samples/eval_set_helm.json
            trace_format: otlp-json
            output: json
            """
        )
    )

    with pytest.raises(ValueError, match="Legacy eval config keys are no longer supported"):
        load_eval_config(config_file)


def test_merge_configs_replaces_evaluators_by_name_and_preserves_file_scalars():
    file_config = EvalRunConfig(
        trace_files=["from-file.json"],
        eval_set_file="from-file-eval-set.json",
        trace_format="jaeger-json",
        output_format="json",
        evaluators=[
            BuiltinMetricDef(name="tool_trajectory_avg_score", threshold=0.5),
            CodeEvaluatorDef(name="custom_eval", path="./examples/custom_evaluators/tool_call_checker.py"),
        ],
    )
    cli_config = EvalRunConfig(
        trace_files=[],
        evaluators=[
            BuiltinMetricDef(name="tool_trajectory_avg_score", threshold=0.9),
            BuiltinMetricDef(name="response_match_score"),
        ],
    )

    merged = merge_configs(file_config, cli_config)

    assert merged.eval_set_file == "from-file-eval-set.json"
    assert merged.trace_format == "jaeger-json"
    assert merged.output_format == "json"
    assert [(e.name, e.type) for e in merged.evaluators] == [
        ("tool_trajectory_avg_score", "builtin"),
        ("custom_eval", "code"),
        ("response_match_score", "builtin"),
    ]
    tool_trajectory = next(e for e in merged.evaluators if e.name == "tool_trajectory_avg_score")
    assert tool_trajectory.threshold == 0.9


def test_eval_params_reject_duplicate_evaluator_names():
    with pytest.raises(ValueError, match="globally unique"):
        EvalParams(
            evaluators=[
                BuiltinMetricDef(name="duplicate_name"),
                CodeEvaluatorDef(name="duplicate_name", path="./examples/custom_evaluators/tool_call_checker.py"),
            ]
        )
