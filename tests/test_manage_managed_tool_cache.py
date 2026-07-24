from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import manage_managed_tool_cache  # noqa: E402
from manage_managed_tool_cache import _assert_bootstrap_runtime  # noqa: E402
from managed_tool_store import (  # noqa: E402
    ManagedToolStoreError,
    resolve_managed_store_roots,
)
from smt_windows import ManagedProcessEnvironmentError  # noqa: E402


def test_maintenance_rejects_managed_python_runtime(tmp_path: Path) -> None:
    roots = resolve_managed_store_roots(tmp_path)
    managed_python = roots.payload / "python" / "entry" / "Scripts" / "python.exe"

    with pytest.raises(ManagedToolStoreError, match="bootstrap Python"):
        _assert_bootstrap_runtime(roots, managed_python)


def test_maintenance_accepts_independent_bootstrap_runtime(tmp_path: Path) -> None:
    roots = resolve_managed_store_roots(tmp_path)
    bootstrap_python = tmp_path / "bootstrap" / "python.exe"

    _assert_bootstrap_runtime(roots, bootstrap_python)


def test_maintenance_cli_maps_platform_safety_error_to_blocked_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable_roots() -> None:
        raise ManagedProcessEnvironmentError("unsafe Local AppData root")

    monkeypatch.setattr(
        manage_managed_tool_cache,
        "resolve_managed_store_roots",
        unavailable_roots,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["manage_managed_tool_cache.py", "inspect"],
    )

    assert manage_managed_tool_cache.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "operation": "inspect",
        "status": "blocked",
        "error": "unsafe Local AppData root",
    }
