"""Imports point down: ``otel`` knows nothing about GenAI, ``genai`` nothing about evaluation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "agentevals"
ALLOWED = {"otel": {"otel"}, "genai": {"otel", "genai"}}


def _imported_packages(path: Path, package: str) -> set[str]:
    """Top level ``agentevals`` packages or modules a module imports, relative or absolute."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                found.add(package)
            elif node.level == 2:
                found.add((node.module or "").split(".")[0] or "agentevals")
            elif node.level == 0 and (node.module or "").startswith("agentevals."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] for a in node.names if a.name.startswith("agentevals.")}
    return found


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_imports_only_lower_layers(package):
    offenders = {
        path.name: sorted(imported - ALLOWED[package])
        for path in sorted((PACKAGE / package).glob("*.py"))
        if (imported := _imported_packages(path, package)) - ALLOWED[package]
    }
    assert offenders == {}
