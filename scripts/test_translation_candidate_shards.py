from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from translation_candidate_shards import record_translation_shard_result, write_translation_candidate_shards


def write_scale_report(root: Path, *, game_id: str = "skyrim-se", batch_rows: int = 2) -> None:
    path = root / "qa" / "Fixture.scale_execution.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "report_type": "mod-scale-execution",
                "status": "ready",
                "mod_name": "Fixture",
                "game_id": game_id,
                "effective": {"translation_batch_rows": batch_rows},
            }
        ),
        encoding="utf-8",
    )


def write_candidates(root: Path, rows: list[dict[str, object]]) -> Path:
    path = root / "out" / "Fixture" / "non_gui_exports" / "translation_candidates_unique.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_scale_batch_rows_create_bounded_context_shards() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root)
        rows = [{"source": f"Source {index}"} for index in range(5)]
        source = write_candidates(root, rows)

        payload = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )

        shard_root = root / "work" / "shards" / "Fixture" / "translation_candidates"
        assert payload["translation_batch_rows"] == 2
        assert payload["shard_count"] == 3
        assert [row["candidate_count"] for row in payload["shards"]] == [2, 2, 1]
        assert len(source.read_text(encoding="utf-8").splitlines()) == 5
        assert all(len(path.read_text(encoding="utf-8").splitlines()) <= 2 for path in shard_root.glob("*.jsonl"))


def test_scale_batch_rows_cannot_exceed_absolute_cap() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root, batch_rows=100_001)
        rows = [{"source": "A"}]
        source = write_candidates(root, rows)

        with pytest.raises(ValueError, match="absolute safety cap"):
            write_translation_candidate_shards(
                root=root,
                mod_name="Fixture",
                game_id="skyrim-se",
                source_jsonl=source,
                rows=rows,
            )


def test_changed_shard_drops_preserved_completion_status() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root)
        rows = [{"source": "A"}, {"source": "B"}, {"source": "C"}]
        source = write_candidates(root, rows)
        first = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        outputs = [
            root / "translated" / "Fixture" / "batch-1.jsonl",
            root / "translated" / "Fixture" / "batch-2.jsonl",
        ]
        outputs[0].parent.mkdir(parents=True)
        for output in outputs:
            output.write_text('{"target":"translated"}\n', encoding="utf-8")
        for index, output in enumerate(outputs):
            relative_output = f"translated/Fixture/batch-{index + 1}.jsonl"
            record_translation_shard_result(
                root=root,
                mod_name="Fixture",
                shard_id=str(first["shards"][index]["shard_id"]),
                status="qa_passed",
                output_values=[relative_output],
            )

        unchanged = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        assert unchanged["shards"][0]["status"] == "qa_passed"
        assert unchanged["shards"][1]["status"] == "qa_passed"

        changed_rows = [{"source": "Changed"}, {"source": "B"}, {"source": "C"}]
        write_candidates(root, changed_rows)
        changed = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=changed_rows,
        )
        assert changed["shards"][0]["status"] == "pending"
        assert changed["shards"][1]["status"] == "qa_passed"


def test_scale_report_game_mismatch_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root, game_id="fallout4")
        rows = [{"source": "A"}]
        source = write_candidates(root, rows)

        with pytest.raises(ValueError, match="not valid for candidate sharding"):
            write_translation_candidate_shards(
                root=root,
                mod_name="Fixture",
                game_id="skyrim-se",
                source_jsonl=source,
                rows=rows,
            )


def test_blocked_shard_is_preserved_without_output_until_source_changes() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root)
        rows = [{"source": "A"}]
        source = write_candidates(root, rows)
        first = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        record_translation_shard_result(
            root=root,
            mod_name="Fixture",
            shard_id=str(first["shards"][0]["shard_id"]),
            status="blocked",
            output_values=[],
        )

        unchanged = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        assert unchanged["shards"][0]["status"] == "blocked"
        assert unchanged["shards"][0]["qa_status"] == "blocked"


def test_shard_result_rejects_mod_input_as_translation_output() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root)
        rows = [{"source": "A"}]
        source = write_candidates(root, rows)
        payload = write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        forbidden = root / "mod" / "Fixture" / "input.txt"
        forbidden.parent.mkdir(parents=True)
        forbidden.write_text("not an output", encoding="utf-8")

        with pytest.raises(ValueError, match="must be a file under translated"):
            record_translation_shard_result(
                root=root,
                mod_name="Fixture",
                shard_id=str(payload["shards"][0]["shard_id"]),
                status="translated",
                output_values=["mod/Fixture/input.txt"],
            )


def test_shard_generation_rejects_corrupt_previous_index() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        write_scale_report(root)
        rows = [{"source": "A"}]
        source = write_candidates(root, rows)
        write_translation_candidate_shards(
            root=root,
            mod_name="Fixture",
            game_id="skyrim-se",
            source_jsonl=source,
            rows=rows,
        )
        index = root / "work" / "shards" / "Fixture" / "translation_candidates" / "index.json"
        index.write_text("{broken", encoding="utf-8")

        with pytest.raises(ValueError, match="index is invalid"):
            write_translation_candidate_shards(
                root=root,
                mod_name="Fixture",
                game_id="skyrim-se",
                source_jsonl=source,
                rows=rows,
            )
