from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import model_usage  # noqa: E402
import new_final_binary_review_packet  # noqa: E402
import new_final_text_review_packet  # noqa: E402
import new_model_review_packet  # noqa: E402
import new_translation_task  # noqa: E402
import translation_candidate_shards  # noqa: E402
import write_workflow_tasks  # noqa: E402
from game_context import game_context_metadata, load_game_profile  # noqa: E402


def _write_input(root: Path, relative: str = "qa/Example.packet.md") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("中文 A\n", encoding="utf-8")
    return path


def _pending(root: Path, usage_id: str) -> dict[str, object]:
    return json.loads(
        (root / ".workflow" / "model_usage_pending" / f"{usage_id}.json").read_text(
            encoding="utf-8"
        )
    )


def test_create_pending_computes_utf8_metrics_and_preserves_unknown_counts(
    tmp_path: Path,
) -> None:
    packet = _write_input(tmp_path)
    raw = packet.read_bytes()

    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-review-fixed",
    )

    assert usage_id == "model-review-fixed"
    payload = _pending(tmp_path, usage_id)
    assert payload["input_sha256"] == hashlib.sha256(raw).hexdigest()
    assert payload["input_bytes"] == len(raw)
    assert payload["input_characters"] == len(raw.decode("utf-8"))
    assert payload["review_groups"] is None
    assert "changed_groups" not in payload
    assert payload["input_path"] == "qa/Example.packet.md"
    assert payload["input_paths"] == ["qa/Example.packet.md"]


def test_create_pending_reuses_an_unfinished_identical_task(tmp_path: Path) -> None:
    _write_input(tmp_path)
    issued = iter(("model-first", "model-second"))

    first = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        review_groups=7,
        usage_id_factory=lambda: next(issued),
    )
    second = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        review_groups=7,
        usage_id_factory=lambda: next(issued),
    )

    assert first == second == "model-first"
    assert len(list((tmp_path / ".workflow" / "model_usage_pending").glob("*.json"))) == 1


def test_create_pending_issues_a_new_attempt_after_completed_identical_task(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    output = tmp_path / "qa" / "completed.md"
    output.write_text("done\n", encoding="utf-8")
    issued = iter(("model-completed-first", "model-completed-second"))
    first = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:final_text",
        stage="final_text_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )
    model_usage.record_usage(
        tmp_path,
        usage_id=first,
        status="completed",
        output_path=output,
        tool="codex",
    )

    second = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:final_text",
        stage="final_text_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )

    assert first == "model-completed-first"
    assert second == "model-completed-second"
    pending, damaged = model_usage.read_pending_records(tmp_path)
    assert damaged == 0
    assert [row["usage_id"] for row in pending] == ["model-completed-second"]
    model_usage.record_usage(
        tmp_path,
        usage_id=second,
        status="completed",
        output_path=output,
        tool="codex",
    )
    rows, damaged = model_usage.read_usage_log(tmp_path)
    assert damaged == 0
    assert [row["usage_id"] for row in rows] == [
        "model-completed-first",
        "model-completed-second",
    ]
    assert "1 次相同 task_id、stage 和输入 SHA256 的额外执行" in (
        model_usage.summarize_usage(tmp_path)
    )


def test_ensure_packet_pending_does_not_reissue_a_completed_identical_packet(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    output = tmp_path / "qa" / "completed.md"
    output.write_text("done\n", encoding="utf-8")
    issued = iter(("model-completed-first", "model-completed-second"))
    first = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:final_text",
        stage="final_text_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )
    model_usage.record_usage(
        tmp_path,
        usage_id=first,
        status="completed",
        output_path=output,
        tool="codex",
    )

    second = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:final_text",
        stage="final_text_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )

    assert first == "model-completed-first"
    assert second is None
    assert model_usage.read_pending_records(tmp_path)[0] == []


def test_create_pending_can_issue_a_new_attempt_for_an_identical_task(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    issued = iter(("model-first-attempt", "model-second-attempt"))

    first = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="recovery:ExampleMod:review",
        stage="model_recovery",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )
    second = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="recovery:ExampleMod:review",
        stage="model_recovery",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
        reuse_existing=False,
    )

    assert first == "model-first-attempt"
    assert second == "model-second-attempt"
    assert len(list((tmp_path / ".workflow" / "model_usage_pending").glob("*.json"))) == 2


def test_concurrent_create_pending_reuses_one_identical_task(tmp_path: Path) -> None:
    _write_input(tmp_path)
    results: list[str | None] = []
    errors: list[BaseException] = []
    sequence = iter(range(8))
    sequence_lock = threading.Lock()

    def create() -> None:
        try:
            def usage_id_factory() -> str:
                with sequence_lock:
                    suffix = next(sequence)
                time.sleep(0.05)
                return f"model-concurrent-create-{suffix}"

            results.append(
                model_usage.create_pending(
                    tmp_path,
                    mod_name="ExampleMod",
                    task_id="review:ExampleMod:initial",
                    stage="initial_review",
                    input_path="qa/Example.packet.md",
                    usage_id_factory=usage_id_factory,
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(results)) == 1
    assert len(list((tmp_path / ".workflow" / "model_usage_pending").glob("*.json"))) == 1


def test_pending_reader_rejects_filename_payload_usage_id_mismatch(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-correct-name",
    )
    pending_dir = tmp_path / ".workflow" / "model_usage_pending"
    (pending_dir / f"{usage_id}.json").rename(pending_dir / "wrong-name.json")

    rows, damaged = model_usage.read_pending_records(tmp_path)

    assert rows == []
    assert damaged == 1


def test_create_pending_rejects_non_model_stage(tmp_path: Path) -> None:
    _write_input(tmp_path)

    with pytest.raises(ValueError, match="stage"):
        model_usage.create_pending(
            tmp_path,
            mod_name="ExampleMod",
            task_id="qa:ExampleMod",
            stage="strict_qa",
            input_path="qa/Example.packet.md",
        )


def test_create_pending_write_failure_warns_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    warnings: list[str] = []

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(model_usage, "_write_pending_atomic", fail_write)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        warning_sink=warnings.append,
    )

    assert usage_id is None
    assert warnings and "disk unavailable" in warnings[0]


def test_ensure_packet_pending_unreadable_usage_log_warns_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    warnings: list[str] = []

    def fail_read(*_args: object, **_kwargs: object) -> tuple[list[dict[str, object]], int]:
        raise model_usage.ModelUsageError("log unreadable")

    monkeypatch.setattr(model_usage, "read_usage_log", fail_read)

    usage_id = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        warning_sink=warnings.append,
    )

    assert usage_id is None
    assert warnings and "log unreadable" in warnings[0]


def test_ensure_packet_pending_unreadable_state_warns_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    warnings: list[str] = []

    def fail_read(
        *_args: object, **_kwargs: object
    ) -> tuple[list[dict[str, object]], int]:
        raise model_usage.ModelUsageError("pending directory is unsafe")

    monkeypatch.setattr(model_usage, "read_pending_records", fail_read)

    usage_id = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        warning_sink=warnings.append,
    )

    assert usage_id is None
    assert warnings and "pending directory is unsafe" in warnings[0]


def test_record_completed_calculates_output_and_cleans_pending(tmp_path: Path) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-completed",
    )
    output = tmp_path / "qa" / "Example.review.md"
    output.write_bytes("通过\n".encode("utf-8"))

    result = model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status="completed",
        output_path="qa/Example.review.md",
        tool="codex",
    )

    rows, damaged = model_usage.read_usage_log(tmp_path)
    assert result.recorded is True
    assert result.already_recorded is False
    assert damaged == 0
    assert len(rows) == 1
    assert rows[0]["output_bytes"] == len(output.read_bytes())
    assert rows[0]["token_measurement"] is None
    assert rows[0]["input_tokens"] is None
    assert rows[0]["output_tokens"] is None
    assert not (
        tmp_path / ".workflow" / "model_usage_pending" / f"{usage_id}.json"
    ).exists()


def test_record_cleanup_failure_returns_recorded_result_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-cleanup-warning",
    )

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(model_usage, "_delete_pending_if_present", fail_cleanup)
    result = model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status="failed",
        output_path=None,
        tool="codex",
    )

    rows, damaged = model_usage.read_usage_log(tmp_path)
    assert result.recorded is True
    assert result.already_recorded is False
    assert result.warnings and "cleanup denied" in result.warnings[0]
    assert damaged == 0
    assert [row["usage_id"] for row in rows] == [usage_id]
    assert (
        tmp_path / ".workflow" / "model_usage_pending" / f"{usage_id}.json"
    ).is_file()


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_record_noncompleted_allows_no_output(tmp_path: Path, status: str) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id=f"review:ExampleMod:{status}",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: f"model-{status}",
    )

    model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status=status,
        output_path=None,
        tool="codex",
    )

    rows, _ = model_usage.read_usage_log(tmp_path)
    assert rows[0]["status"] == status
    assert rows[0]["output_bytes"] is None


def test_record_completed_rejects_missing_output_and_keeps_pending(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-missing-output",
    )

    with pytest.raises(model_usage.ModelUsageError, match="output"):
        model_usage.record_usage(
            tmp_path,
            usage_id=usage_id,
            status="completed",
            output_path="qa/missing.md",
            tool="codex",
        )

    assert (
        tmp_path
        / ".workflow"
        / "model_usage_pending"
        / f"{usage_id}.json"
    ).is_file()
    assert not (tmp_path / "qa" / "model_usage.jsonl").exists()


def test_record_provider_tokens_and_idempotent_retry(tmp_path: Path) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="translation:ExampleMod:main",
        stage="translation",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-token",
    )
    output = tmp_path / "translated" / "Example.jsonl"
    output.parent.mkdir()
    output.write_text("{}\n", encoding="utf-8")

    first = model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status="completed",
        output_path="translated/Example.jsonl",
        tool="opencode",
        model="gpt-5.6",
        input_tokens=48210,
        output_tokens=6210,
    )
    output.unlink()
    second = model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status="completed",
        output_path="translated/Example.jsonl",
        tool="opencode",
        model="gpt-5.6",
        input_tokens=48210,
        output_tokens=6210,
    )

    rows, _ = model_usage.read_usage_log(tmp_path)
    assert first.recorded is True
    assert second.recorded is False
    assert second.already_recorded is True
    assert len(rows) == 1
    assert rows[0]["token_measurement"] == "provider_reported"
    assert rows[0]["input_tokens"] == 48210
    assert rows[0]["output_tokens"] == 6210


def test_record_missing_pending_does_not_fabricate_log_row(tmp_path: Path) -> None:
    with pytest.raises(model_usage.ModelUsageError, match="pending"):
        model_usage.record_usage(
            tmp_path,
            usage_id="model-does-not-exist",
            status="failed",
            output_path=None,
            tool="codex",
        )

    assert not (tmp_path / "qa" / "model_usage.jsonl").exists()


def test_record_ignores_parseable_invalid_row_with_matching_usage_id(
    tmp_path: Path,
) -> None:
    _write_input(tmp_path)
    output = tmp_path / "qa" / "output.md"
    output.write_text("done\n", encoding="utf-8")
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-invalid-existing-row",
    )
    log = tmp_path / "qa" / "model_usage.jsonl"
    log.write_text(json.dumps({"usage_id": usage_id}) + "\n", encoding="utf-8")

    result = model_usage.record_usage(
        tmp_path,
        usage_id=usage_id,
        status="completed",
        output_path=output,
        tool="codex",
    )
    rows, damaged = model_usage.read_usage_log(tmp_path)

    assert result.recorded is True
    assert result.already_recorded is False
    assert damaged == 1
    assert len(rows) == 1
    assert rows[0]["usage_id"] == usage_id
    assert rows[0]["status"] == "completed"


def test_record_write_failure_keeps_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-write-failure",
    )

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("write denied")

    monkeypatch.setattr(model_usage, "_append_usage_row", fail_append)
    with pytest.raises(model_usage.ModelUsageError, match="write failed"):
        model_usage.record_usage(
            tmp_path,
            usage_id=usage_id,
            status="failed",
            output_path=None,
            tool="codex",
        )

    assert (
        tmp_path / ".workflow" / "model_usage_pending" / f"{usage_id}.json"
    ).is_file()
    assert (
        tmp_path / ".workflow" / "model_usage_retired" / f"{usage_id}.json"
    ).is_file()


def test_record_write_failure_retires_pending_from_workflow_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-retired-write-failure",
    )

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("write denied")

    monkeypatch.setattr(model_usage, "_append_usage_row", fail_append)
    with pytest.raises(model_usage.ModelUsageUnavailable):
        model_usage.record_usage(
            tmp_path,
            usage_id=usage_id,
            status="failed",
            output_path=None,
            tool="codex",
        )

    tasks, _issues = write_workflow_tasks.pending_model_usage_tasks(tmp_path)
    assert tasks == []
    assert "0 个 pending 尚未生成日志" in model_usage.summarize_usage(tmp_path)
    assert (
        tmp_path / ".workflow" / "model_usage_pending" / f"{usage_id}.json"
    ).is_file()


def test_retired_identical_packet_is_not_reissued_by_ensure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    issued = iter(("model-retired-first", "model-retired-second"))
    first = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("write denied")

    monkeypatch.setattr(model_usage, "_append_usage_row", fail_append)
    with pytest.raises(model_usage.ModelUsageUnavailable):
        model_usage.record_usage(
            tmp_path,
            usage_id=first,
            status="failed",
            output_path=None,
            tool="codex",
        )

    second = model_usage.ensure_packet_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: next(issued),
    )

    assert second is None
    pending, damaged = model_usage.read_pending_records(tmp_path)
    assert damaged == 0
    assert [row["usage_id"] for row in pending] == ["model-retired-first"]


def test_record_lock_failure_retires_pending_from_workflow_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-retired-lock-failure",
    )

    def fail_lock(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("lock unavailable")

    monkeypatch.setattr(model_usage.ResourceLock, "acquire", fail_lock)
    with pytest.raises(model_usage.ModelUsageUnavailable):
        model_usage.record_usage(
            tmp_path,
            usage_id=usage_id,
            status="failed",
            output_path=None,
            tool="codex",
        )

    tasks, _issues = write_workflow_tasks.pending_model_usage_tasks(tmp_path)
    assert tasks == []
    assert (
        tmp_path / ".workflow" / "model_usage_pending" / f"{usage_id}.json"
    ).is_file()
    assert (
        tmp_path / ".workflow" / "model_usage_retired" / f"{usage_id}.json"
    ).is_file()


def test_cli_treats_transient_log_failure_as_nonblocking_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(model_usage, "project_root", lambda: Path.cwd())

    def unavailable(*_args: object, **_kwargs: object) -> model_usage.RecordResult:
        raise model_usage.ModelUsageUnavailable("lock timed out")

    monkeypatch.setattr(model_usage, "record_usage", unavailable)

    exit_code = model_usage.main(
        [
            "record",
            "--usage-id",
            "model-warning",
            "--status",
            "failed",
            "--tool",
            "codex",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "WARNING: lock timed out" in captured.err


def test_cli_prints_cleanup_warning_and_returns_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(model_usage, "project_root", lambda: Path.cwd())
    monkeypatch.setattr(
        model_usage,
        "record_usage",
        lambda *_args, **_kwargs: model_usage.RecordResult(
            recorded=True,
            already_recorded=False,
            usage_id="model-cleanup-warning",
            warnings=("model usage recorded but pending cleanup failed",),
        ),
    )

    exit_code = model_usage.main(
        [
            "record",
            "--usage-id",
            "model-cleanup-warning",
            "--status",
            "failed",
            "--tool",
            "codex",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Model usage recorded: model-cleanup-warning" in captured.out
    assert "WARNING: model usage recorded but pending cleanup failed" in captured.err


def test_concurrent_record_writes_one_row(tmp_path: Path) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-concurrent",
    )
    results: list[model_usage.RecordResult] = []
    errors: list[BaseException] = []

    def record() -> None:
        try:
            results.append(
                model_usage.record_usage(
                    tmp_path,
                    usage_id=usage_id,
                    status="failed",
                    output_path=None,
                    tool="codex",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=record) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows, _ = model_usage.read_usage_log(tmp_path)
    assert errors == []
    assert len(rows) == 1
    assert sum(result.recorded for result in results) == 1
    assert sum(result.already_recorded for result in results) == 1


def test_create_waits_until_record_appends_and_retires_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_input(tmp_path)
    output = tmp_path / "qa" / "completed.md"
    output.write_text("done\n", encoding="utf-8")
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-interleaved-first",
    )
    delete_entered = threading.Event()
    allow_delete = threading.Event()
    original_delete = model_usage._delete_pending_if_present

    def delayed_delete(root: Path, current_usage_id: str) -> None:
        delete_entered.set()
        assert allow_delete.wait(timeout=5)
        original_delete(root, current_usage_id)

    monkeypatch.setattr(model_usage, "_delete_pending_if_present", delayed_delete)
    record_errors: list[BaseException] = []
    ensure_errors: list[BaseException] = []
    ensured: list[str | None] = []

    def record() -> None:
        try:
            model_usage.record_usage(
                tmp_path,
                usage_id=usage_id,
                status="completed",
                output_path=output,
                tool="codex",
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            record_errors.append(exc)

    def ensure() -> None:
        try:
            ensured.append(
                model_usage.ensure_packet_pending(
                    tmp_path,
                    mod_name="ExampleMod",
                    task_id="review:ExampleMod:initial",
                    stage="initial_review",
                    input_path="qa/Example.packet.md",
                    usage_id_factory=lambda: "model-interleaved-second",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports it
            ensure_errors.append(exc)

    record_thread = threading.Thread(target=record)
    record_thread.start()
    assert delete_entered.wait(timeout=5)
    ensure_thread = threading.Thread(target=ensure)
    ensure_thread.start()
    time.sleep(0.1)
    assert ensure_thread.is_alive()
    allow_delete.set()
    record_thread.join(timeout=5)
    ensure_thread.join(timeout=5)

    assert record_errors == []
    assert ensure_errors == []
    assert ensured == [None]


def test_record_rejects_partial_token_pair(tmp_path: Path) -> None:
    _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="translation:ExampleMod:main",
        stage="translation",
        input_path="qa/Example.packet.md",
        usage_id_factory=lambda: "model-partial-token",
    )

    with pytest.raises(model_usage.ModelUsageError, match="together"):
        model_usage.record_usage(
            tmp_path,
            usage_id=usage_id,
            status="failed",
            output_path=None,
            tool="pi",
            input_tokens=4,
        )


def test_summary_groups_stages_duplicates_tokens_pending_and_damage(
    tmp_path: Path,
) -> None:
    pending_dir = tmp_path / ".workflow" / "model_usage_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "model-pending.json").write_text(
        json.dumps(
                {
                    "schema_version": 1,
                    "usage_id": "model-pending",
                    "created_at": "2026-07-24T12:00:00Z",
                    "mod_name": "ExampleMod",
                "task_id": "review:ExampleMod:final_binary",
                "stage": "final_binary_review",
                "input_path": "qa/binary.md",
                "input_sha256": "b" * 64,
                "input_bytes": 100,
                "input_characters": 100,
                "review_groups": 2,
            }
        ),
        encoding="utf-8",
    )
    log = tmp_path / "qa" / "model_usage.jsonl"
    log.parent.mkdir()
    base = {
        "schema_version": 1,
        "timestamp": "2026-07-24T12:10:00Z",
        "mod_name": "ExampleMod",
        "task_id": "review:ExampleMod:initial",
        "stage": "initial_review",
        "status": "completed",
        "tool": "codex",
        "model": None,
        "input_sha256": "a" * 64,
        "input_bytes": 1024,
        "input_characters": 900,
        "output_bytes": 512,
        "review_groups": 7,
        "token_measurement": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    rows = [
        {**base, "usage_id": "model-1"},
        {
            **base,
            "usage_id": "model-2",
            "status": "failed",
            "output_bytes": None,
            "token_measurement": "provider_reported",
            "input_tokens": 50,
            "output_tokens": 10,
        },
    ]
    log.write_text(
        "\n".join((json.dumps(rows[0]), "{broken", json.dumps(rows[1]))) + "\n",
        encoding="utf-8",
    )

    text = model_usage.summarize_usage(tmp_path, mod_name="ExampleMod")

    assert "ExampleMod 模型工作负载" in text
    assert "初次模型审查：" in text
    assert "执行尝试：2" in text
    assert "输入任务包：2 KB" in text
    assert "疑似重复提交：" in text
    assert "1 次相同 task_id、stage 和输入 SHA256 的额外执行" in text
    assert "覆盖：1 / 2 次执行" in text
    assert "输入 Token：50" in text
    assert "待记录任务：" in text
    assert "1 个 pending 尚未生成日志" in text
    assert "损坏日志行：1" in text


def test_summary_skips_invalid_utf8_log_lines(tmp_path: Path) -> None:
    log = tmp_path / "qa" / "model_usage.jsonl"
    log.parent.mkdir()
    log.write_bytes(b"\xff\xfe\n")

    text = model_usage.summarize_usage(tmp_path)

    assert "损坏日志行：1" in text
    assert "执行尝试" not in text


def test_pending_reader_skips_invalid_utf8_files(tmp_path: Path) -> None:
    pending_dir = tmp_path / ".workflow" / "model_usage_pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "model-invalid-utf8.json").write_bytes(b"\xff")

    rows, damaged = model_usage.read_pending_records(tmp_path)

    assert rows == []
    assert damaged == 1


def test_usage_log_rejects_a_hardlinked_internal_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    log = tmp_path / "qa" / "model_usage.jsonl"
    log.parent.mkdir()
    try:
        os.link(outside, log)
    except OSError as exc:  # pragma: no cover - unsupported file system
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(model_usage.ModelUsageError, match="hardlink"):
        model_usage.read_usage_log(tmp_path)


def test_pending_reader_rejects_a_symlinked_internal_directory(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-pending"
    outside.mkdir()
    pending_dir = tmp_path / ".workflow" / "model_usage_pending"
    pending_dir.parent.mkdir()
    try:
        pending_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(model_usage.ModelUsageError, match="symlink|reparse"):
        model_usage.read_pending_records(tmp_path)


def test_translation_task_packet_issues_translation_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "mod" / "readme.txt"
    source.parent.mkdir()
    source.write_text("Translate me", encoding="utf-8")
    monkeypatch.setattr(new_translation_task, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "new_translation_task.py",
            "--mod-name",
            "ExampleMod",
            "--source-file",
            "mod/readme.txt",
        ],
    )

    assert new_translation_task.main() == 0

    pending, damaged = model_usage.read_pending_records(tmp_path)
    assert damaged == 0
    assert len(pending) == 1
    assert pending[0]["stage"] == "translation"
    assert pending[0]["task_id"] == "translation:ExampleMod:main"
    assert pending[0]["input_path"] == "work/tasks/ExampleMod/task.md"
    assert pending[0]["input_paths"] == [
        "work/tasks/ExampleMod/task.md",
        "mod/readme.txt",
    ]
    task_raw = (tmp_path / "work" / "tasks" / "ExampleMod" / "task.md").read_bytes()
    source_raw = source.read_bytes()
    assert pending[0]["input_bytes"] == len(task_raw) + len(source_raw)
    assert pending[0]["input_characters"] == len(task_raw.decode("utf-8"))


def test_translation_task_accepts_utf16_source_for_byte_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "mod" / "Interface" / "translations" / "example_english.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\xff\xfe" + "$HELLO\tHello\r\n".encode("utf-16-le"))
    monkeypatch.setattr(new_translation_task, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "new_translation_task.py",
            "--mod-name",
            "ExampleMod",
            "--source-file",
            "mod/Interface/translations/example_english.txt",
        ],
    )

    assert new_translation_task.main() == 0

    pending, damaged = model_usage.read_pending_records(tmp_path)
    task_raw = (tmp_path / "work" / "tasks" / "ExampleMod" / "task.md").read_bytes()
    assert damaged == 0
    assert len(pending) == 1
    assert pending[0]["input_paths"] == [
        "work/tasks/ExampleMod/task.md",
        "mod/Interface/translations/example_english.txt",
    ]
    assert pending[0]["input_bytes"] == len(task_raw) + len(source.read_bytes())
    assert pending[0]["input_characters"] == len(task_raw.decode("utf-8"))


def test_translation_candidate_shards_issue_one_pending_per_model_batch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "out" / "ExampleMod" / "qa" / "candidates.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text('{"source":"Hello"}\n', encoding="utf-8")

    payload = translation_candidate_shards.write_translation_candidate_shards(
        root=tmp_path,
        mod_name="ExampleMod",
        game_id="skyrim-se",
        source_jsonl=source,
        rows=[{"source": "Hello"}],
    )

    pending, damaged = model_usage.read_pending_records(tmp_path)
    assert payload["shard_count"] == 1
    assert damaged == 0
    assert len(pending) == 1
    assert pending[0]["stage"] == "translation"
    assert (
        pending[0]["task_id"]
        == "translation:ExampleMod:translation-candidates-00001"
    )
    assert pending[0]["review_groups"] == 1


def test_initial_review_packet_issues_pending_with_group_count(tmp_path: Path) -> None:
    packet = tmp_path / "qa" / "Example.model_review_packet.md"
    review = tmp_path / "qa" / "Example.model_review.md"

    new_model_review_packet.write_packet(
        tmp_path,
        "ExampleMod",
        packet,
        review,
        [
            {
                "File": "work/example.jsonl",
                "Line": 1,
                "Type": "text",
                "Risk": "safe",
                "Context": "menu",
                "Source": "Hello",
                "Target": "你好",
            }
        ],
    )

    pending, _ = model_usage.read_pending_records(tmp_path)
    assert len(pending) == 1
    assert pending[0]["stage"] == "initial_review"
    assert pending[0]["task_id"] == "review:ExampleMod:initial"
    assert pending[0]["review_groups"] == 1
    assert pending[0]["input_path"] == "qa/Example.model_review_packet.md"
    assert "- Created at:" not in packet.read_text(encoding="utf-8")


def test_review_packet_can_explicitly_issue_model_recovery_pending(
    tmp_path: Path,
) -> None:
    packet = tmp_path / "qa" / "Example.recovery_packet.md"
    review = tmp_path / "qa" / "Example.model_review.md"

    new_model_review_packet.write_packet(
        tmp_path,
        "ExampleMod",
        packet,
        review,
        [],
        usage_stage="model_recovery",
        usage_task_id="recovery:ExampleMod:terminology",
    )

    pending, _ = model_usage.read_pending_records(tmp_path)
    assert len(pending) == 1
    assert pending[0]["stage"] == "model_recovery"
    assert pending[0]["task_id"] == "recovery:ExampleMod:terminology"


def test_final_review_packet_producers_issue_distinct_pending(tmp_path: Path) -> None:
    workspace = tmp_path / "work" / "extracted_mods" / "ExampleMod"
    final_mod = tmp_path / "out" / "ExampleMod" / "汉化产出" / "final_mod"
    workspace.mkdir(parents=True)
    final_mod.mkdir(parents=True)
    text_packet = tmp_path / "qa" / "Example.final_text_review_packet.md"
    text_items = tmp_path / "qa" / "Example.final_text_review_items.jsonl"
    binary_packet = tmp_path / "qa" / "Example.final_binary_review_packet.md"
    binary_items = tmp_path / "qa" / "Example.final_binary_review_items.jsonl"
    review_item = new_final_text_review_packet.ReviewItem(
        File="Interface/translations/example_english.txt",
        Kind="text-line",
        Context="line 1",
        Source="Hello",
        Final="你好",
    )

    new_final_text_review_packet.write_packet(
        tmp_path,
        "ExampleMod",
        workspace,
        final_mod,
        text_packet,
        text_items,
        1,
        [review_item],
    )
    new_final_binary_review_packet.write_reports(
        tmp_path,
        "ExampleMod",
        workspace,
        final_mod,
        binary_packet,
        binary_items,
        0,
        0,
        [],
        [],
        load_game_profile("skyrim-se"),
    )

    pending, _ = model_usage.read_pending_records(tmp_path)
    assert {(row["stage"], row["task_id"]) for row in pending} == {
        ("final_text_review", "review:ExampleMod:final_text"),
        ("final_binary_review", "review:ExampleMod:final_binary"),
    }


def test_workflow_tasks_associate_existing_pending_without_issuing_new(
    tmp_path: Path,
) -> None:
    (tmp_path / ".skyrim-chs-workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "bethesda-mod-chs-translation-workspace",
                "game_id": "skyrim-se",
            }
        ),
        encoding="utf-8",
    )
    packet = _write_input(tmp_path)
    usage_id = model_usage.create_pending(
        tmp_path,
        mod_name="ExampleMod",
        task_id="review:ExampleMod:initial",
        stage="initial_review",
        input_path=packet,
        review_groups=3,
        usage_id_factory=lambda: "model-task-link",
    )
    state_path = tmp_path / "qa" / "workflow_state.json"
    state_path.write_text(
        json.dumps(
            {
                **game_context_metadata(load_game_profile("skyrim-se")),
                "schema_version": 1,
                "generated_at": "2026-07-24T12:00:00",
                "states": [],
            }
        ),
        encoding="utf-8",
    )
    tasks_path = tmp_path / "qa" / "workflow_tasks.json"

    payload, issues = write_workflow_tasks.build_tasks(
        tmp_path, state_path, tasks_path
    )

    assert not [issue for issue in issues if issue.severity == "error"]
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["usage_id"] == usage_id
    assert payload["tasks"][0]["model_task_id"] == "review:ExampleMod:initial"
    assert payload["tasks"][0]["evidence"] == "qa/Example.packet.md"
    assert len(model_usage.read_pending_records(tmp_path)[0]) == 1
