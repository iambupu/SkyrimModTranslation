from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import managed_tool_provisioning as provisioning  # noqa: E402
from managed_tool_store import (  # noqa: E402
    entry_lock,
    ensure_store_layout,
    make_entry_manifest,
    make_tool_key,
    publish_movable_entry,
    resolve_managed_store_roots,
    validate_entry,
)
from smt_windows import SmtLockTimeoutError  # noqa: E402


def _publish_worker(
    base: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    roots = resolve_managed_store_roots(Path(base))
    ensure_store_layout(roots)
    key = make_tool_key("decoder-race", {"version": "1"})
    staging = roots.staging / f"worker-{os.getpid()}"
    staging.mkdir()
    (staging / "tool.exe").write_bytes(b"same-payload")
    manifest = make_entry_manifest(
        key=key,
        entry_root=staging,
        source={"type": "race-test"},
        critical_entries=("tool.exe",),
        producer_version="test",
    )
    start.wait(10)
    try:
        published = publish_movable_entry(roots, staging, manifest)
        results.put(("ok", str(published)))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _hold_lock_worker(
    base: str,
    digest: str,
    mode: str,
    acquired: multiprocessing.queues.Queue,
    release: multiprocessing.synchronize.Event,
) -> None:
    roots = resolve_managed_store_roots(Path(base))
    ensure_store_layout(roots)
    with entry_lock(
        roots,
        "lock-test",
        digest,
        mode=mode,  # type: ignore[arg-type]
        timeout_seconds=5.0,
        command="multiprocess test",
    ):
        acquired.put(digest)
        release.wait(10)


def _provision_decoder_worker(
    base: str,
    archive_path: str,
    archive_sha256: str,
    start: multiprocessing.synchronize.Event,
    downloads: multiprocessing.queues.Queue,
    results: multiprocessing.queues.Queue,
) -> None:
    roots = resolve_managed_store_roots(Path(base))
    provisioning.GITHUB_ARCHIVES["RaceTool"] = {
        "ref": "race-ref",
        "url": "https://example.invalid/race-tool.zip",
        "sha256": archive_sha256,
        "entry_point": "tool.py",
    }

    def downloader(_url: str, target: Path, _allowed_root: Path) -> None:
        downloads.put(os.getpid())
        shutil.copy2(archive_path, target)

    start.wait(10)
    try:
        tool = provisioning.provision_decoder_archive(
            roots,
            "RaceTool",
            downloader=downloader,
        )
        results.put(("ok", tool.key.entry_id))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 LockFileEx")
def test_same_key_concurrent_publication_has_one_healthy_winner(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_publish_worker,
            args=(str(tmp_path), start, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    rows = [results.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert [status for status, _value in rows] == ["ok", "ok"]
    assert len({value for _status, value in rows}) == 1
    roots = resolve_managed_store_roots(tmp_path)
    key = make_tool_key("decoder-race", {"version": "1"})
    assert validate_entry(
        roots,
        key.tool_kind,
        key.key_digest,
        deep=True,
    ).healthy
    assert len(list((roots.entries / key.tool_kind).iterdir())) == 1


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 LockFileEx")
def test_same_key_concurrent_provisioning_downloads_only_once(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "race-tool.zip"
    with ZipFile(archive, "w") as payload:
        payload.writestr("RaceTool-race-ref/tool.py", "payload")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    downloads = context.Queue()
    results = context.Queue()
    workers = [
        context.Process(
            target=_provision_decoder_worker,
            args=(
                str(tmp_path),
                str(archive),
                digest,
                start,
                downloads,
                results,
            ),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    rows = [results.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    assert [status for status, _value in rows] == ["ok", "ok"]
    assert len({value for _status, value in rows}) == 1
    assert downloads.get(timeout=5) in {worker.pid for worker in workers}
    with pytest.raises(queue.Empty):
        downloads.get(timeout=0.2)


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 LockFileEx")
def test_different_keys_can_hold_exclusive_locks_in_parallel(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Queue()
    release = context.Event()
    digests = ("1" * 64, "2" * 64)
    workers = [
        context.Process(
            target=_hold_lock_worker,
            args=(str(tmp_path), digest, "exclusive", acquired, release),
        )
        for digest in digests
    ]
    for worker in workers:
        worker.start()
    observed = {acquired.get(timeout=10), acquired.get(timeout=10)}
    assert observed == set(digests)
    release.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 LockFileEx")
def test_shared_runtime_lease_blocks_delete_and_process_exit_releases_it(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    acquired = context.Queue()
    release = context.Event()
    digest = "3" * 64
    worker = context.Process(
        target=_hold_lock_worker,
        args=(str(tmp_path), digest, "shared", acquired, release),
    )
    worker.start()
    assert acquired.get(timeout=10) == digest
    roots = resolve_managed_store_roots(tmp_path)
    with pytest.raises(SmtLockTimeoutError):
        with entry_lock(
            roots,
            "lock-test",
            digest,
            mode="exclusive",
            timeout_seconds=0.1,
            command="delete test",
        ):
            pass
    release.set()
    worker.join(10)
    assert worker.exitcode == 0
    with entry_lock(
        roots,
        "lock-test",
        digest,
        mode="exclusive",
        timeout_seconds=1.0,
        command="released test",
    ):
        pass
    assert (roots.locks / "entries").is_dir()
