from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from invoke_mutagen_plugin_text_tool import validate_translation_schema  # noqa: E402


def test_validate_translation_schema_accepts_schema_v2(tmp_path: Path) -> None:
    path = tmp_path / "translations.jsonl"
    path.write_text(
        json.dumps({"schema_version": 2, "source": "Name", "target": "名称"}) + "\n",
        encoding="utf-8",
    )

    validate_translation_schema(path)


@pytest.mark.parametrize("schema_version", [None, 0, 1, 3, "2"])
def test_validate_translation_schema_rejects_non_v2_rows(
    tmp_path: Path,
    schema_version: object,
) -> None:
    path = tmp_path / "translations.jsonl"
    path.write_text(
        json.dumps({"schema_version": schema_version, "target": "名称"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version"):
        validate_translation_schema(path)
