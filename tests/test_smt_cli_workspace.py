from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import smt_fingerprint  # noqa: E402
import smt_windows  # noqa: E402
from game_context import load_game_profile  # noqa: E402
from smt_fingerprint import (  # noqa: E402
    FileIdentity,
    FinalizedModName,
    InputChangedError,
    InputEntry,
    InputManifest,
    InputSafetyError,
    UnsupportedInputError,
    build_input_manifest,
    choose_workspace_name,
    composite_input_identity,
    derive_mod_name_candidate,
    finalize_mod_name,
    verify_imported_copy,
    verify_source_unchanged,
)
from smt_windows import (  # noqa: E402
    ManagedProcess,
    ManagedProcessEnvironmentError,
    ManagedProcessTimeoutError,
    SmtLockTimeoutError,
    SmtProcessFileLock,
    get_documents_path,
    get_local_app_data_path,
    start_managed_process,
)


@pytest.fixture
def safe_tmp_path() -> Path:
    with tempfile.TemporaryDirectory(prefix=".pytest-smt-", dir=ROOT) as temp_dir:
        yield Path(temp_dir)


def _directory_contract(entries: tuple[InputEntry, ...]) -> str:
    payload = bytearray(b"SMT-INPUT-DIR\x00")
    payload.extend(struct.pack(">H", 1))
    payload.extend(struct.pack(">Q", len(entries)))
    for entry in entries:
        relative_bytes = entry.relative_path.encode("utf-8")
        payload.extend(b"\x01" if entry.entry_type == "directory" else b"\x02")
        payload.extend(struct.pack(">I", len(relative_bytes)))
        payload.extend(relative_bytes)
        if entry.entry_type == "file":
            payload.extend(struct.pack(">Q", entry.size))
            payload.extend(bytes.fromhex(entry.sha256 or ""))
    return hashlib.sha256(payload).hexdigest()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_directory_manifest_is_stable_and_includes_empty_directory(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example"
    (source / "Empty").mkdir(parents=True)
    (source / "Interface").mkdir()
    (source / "Interface" / "menu.txt").write_text("hello", encoding="utf-8")

    first = build_input_manifest(source)
    second = build_input_manifest(source)

    assert first == second
    assert first.source_kind == "directory"
    assert first.source_identity is not None
    assert isinstance(first.entries, tuple)
    assert [row.relative_path for row in first.entries] == [
        "Empty",
        "Interface",
        "Interface/menu.txt",
    ]
    assert first.entries[0].size == 0
    assert first.entries[0].sha256 is None
    assert first.entries[0].identity is not None
    assert first.entries[2].identity is not None
    assert first.digest == _directory_contract(first.entries)


def test_manifest_is_frozen_and_converts_entries_to_tuple() -> None:
    manifest = InputManifest(
        source_kind="directory",
        entries=[],
        digest="0" * 64,
        source_identity=None,
    )

    assert manifest.entries == ()
    with pytest.raises(FrozenInstanceError):
        manifest.digest = "1" * 64  # type: ignore[misc]


def test_file_identity_has_the_frozen_public_field_contract() -> None:
    identity = FileIdentity(device=1, inode=2, size=3, mtime_ns=4)

    assert [field.name for field in fields(FileIdentity)] == [
        "device",
        "inode",
        "size",
        "mtime_ns",
    ]
    assert identity == FileIdentity(device=1, inode=2, size=3, mtime_ns=4)
    with pytest.raises(FrozenInstanceError):
        identity.device = 5  # type: ignore[misc]


def test_composite_identity_includes_game_and_source_kind(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example"
    source.mkdir()
    manifest = build_input_manifest(source)

    skyrim = composite_input_identity("skyrim-se", manifest)
    fallout = composite_input_identity("fallout4", manifest)

    assert skyrim == f"smt-input-v1:skyrim-se:directory:{manifest.digest}"
    assert fallout == f"smt-input-v1:fallout4:directory:{manifest.digest}"
    assert skyrim != fallout


def test_directory_paths_are_nfc_posix_and_sorted_by_utf8_bytes(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Unicode"
    source.mkdir()
    decomposed = "e\u0301"
    (source / decomposed).mkdir()
    (source / "z").mkdir()
    (source / decomposed / "a.txt").write_text("a", encoding="utf-8")

    manifest = build_input_manifest(source)

    expected_composed = unicodedata.normalize("NFC", decomposed)
    assert [entry.relative_path for entry in manifest.entries] == [
        "z",
        expected_composed,
        f"{expected_composed}/a.txt",
    ]
    assert [entry.relative_path.encode("utf-8") for entry in manifest.entries] == sorted(
        entry.relative_path.encode("utf-8") for entry in manifest.entries
    )


def test_casefold_collision_is_rejected_when_filesystem_can_construct_it(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Collision"
    source.mkdir()
    (source / "Straße").mkdir()
    (source / "STRASSE").mkdir(exist_ok=True)
    names = {entry.name for entry in source.iterdir()}
    if names != {"Straße", "STRASSE"}:
        pytest.skip("filesystem cannot construct both casefold-colliding paths")

    with pytest.raises(InputSafetyError, match="case-insensitive path collision"):
        build_input_manifest(source)


@pytest.mark.parametrize("suffix", [".rar", ".esp", ".esm", ".esl", ".bsa", ".ba2", ".txt"])
def test_unsupported_top_level_file_types_are_rejected(
    safe_tmp_path: Path,
    suffix: str,
) -> None:
    source = safe_tmp_path / f"Example{suffix}"
    source.write_bytes(b"fixture")

    with pytest.raises(UnsupportedInputError):
        build_input_manifest(source)


@pytest.mark.parametrize("marker", ["SteamLibrary", "ModOrganizer", "Vortex"])
def test_generic_risky_locations_are_rejected_before_reading(
    safe_tmp_path: Path,
    marker: str,
) -> None:
    source = safe_tmp_path / marker / "Example.zip"
    source.parent.mkdir()
    source.write_bytes(b"fixture")

    with pytest.raises(InputSafetyError, match="forbidden"):
        build_input_manifest(source)


def test_profile_specific_risky_location_is_rejected_before_reading(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Fallout 4" / "Data" / "Example.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture")

    with pytest.raises(InputSafetyError, match="Fallout 4"):
        build_input_manifest(source, context=load_game_profile("fallout4"))


def test_top_level_symlink_is_rejected_without_following_target(safe_tmp_path: Path) -> None:
    target = safe_tmp_path / "real.zip"
    target.write_bytes(b"archive bytes")
    source = safe_tmp_path / "linked.zip"
    try:
        source.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="symlink|reparse"):
        build_input_manifest(source)

    assert target.read_bytes() == b"archive bytes"


def test_symlink_in_directory_is_rejected_when_supported(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Symlink"
    source.mkdir()
    target = source / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = source / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="symlink|reparse"):
        build_input_manifest(source)


def test_directory_replacement_after_discovery_is_rejected_before_acceptance(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "DirectoryRace"
    child = source / "child"
    child.mkdir(parents=True)
    outside = safe_tmp_path / "outside-empty"
    outside.mkdir()
    real_scandir = os.scandir
    replaced = False

    def replacing_scandir(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        nonlocal replaced
        if Path(path) == child and not replaced:
            replaced = True
            child.rmdir()
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlink race construction is unavailable: {exc}")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", replacing_scandir)
    try:
        with pytest.raises(InputSafetyError, match="changed|symlink|reparse"):
            build_input_manifest(source)
    finally:
        if child.is_symlink():
            child.unlink()

    assert replaced


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-specific")
def test_directory_junction_is_rejected_when_supported(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "DirectoryReparse"
    source.mkdir()
    target = safe_tmp_path / "outside-directory"
    target.mkdir()
    (target / "secret.txt").write_text("must not be read", encoding="utf-8")
    junction = source / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"NTFS junction creation is unavailable: {result.stderr or result.stdout}")
    try:
        with pytest.raises(InputSafetyError, match="junction|reparse"):
            build_input_manifest(source)
        assert (target / "secret.txt").read_text(encoding="utf-8") == "must not be read"
    finally:
        os.rmdir(junction)


def test_multiple_hardlinks_in_directory_are_rejected(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Hardlinks"
    source.mkdir()
    original = source / "one.txt"
    original.write_text("same inode", encoding="utf-8")
    try:
        os.link(original, source / "two.txt")
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="hardlinks"):
        build_input_manifest(source)


def test_archive_with_multiple_hardlinks_is_rejected(safe_tmp_path: Path) -> None:
    original = safe_tmp_path / "original.zip"
    original.write_bytes(b"archive")
    linked = safe_tmp_path / "linked.zip"
    try:
        os.link(original, linked)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")

    with pytest.raises(InputSafetyError, match="hardlinks"):
        build_input_manifest(linked)


def test_non_regular_entry_is_rejected(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Special"
    source.mkdir()
    special_path = source / "non-regular"
    if hasattr(os, "mkfifo"):
        os.mkfifo(special_path)
        with pytest.raises(InputSafetyError, match="non-regular"):
            build_input_manifest(source)
        return
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("neither FIFO nor AF_UNIX filesystem sockets are available")

    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            unix_socket.bind(str(special_path))
        except OSError as exc:
            pytest.skip(f"AF_UNIX filesystem socket creation is unavailable: {exc}")
        with pytest.raises(InputSafetyError, match="non-regular"):
            build_input_manifest(source)
    finally:
        unix_socket.close()
        special_path.unlink(missing_ok=True)


def test_hashing_detects_file_change_during_read(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "Changing"
    source.mkdir()
    file_path = source / "large.bin"
    original_size = 1024 * 1024 + 1
    file_path.write_bytes(b"A" * original_size)
    real_reader = smt_fingerprint._read_file_chunks
    changed = False

    def changing_reader(path: Path):
        nonlocal changed
        for chunk in real_reader(path):
            yield chunk
            if not changed:
                changed = True
                path.write_bytes(b"B" * original_size)

    monkeypatch.setattr(smt_fingerprint, "_read_file_chunks", changing_reader)

    with pytest.raises(InputChangedError, match="changed while hashing"):
        build_input_manifest(source)


def test_archive_hashing_detects_change_during_read(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = safe_tmp_path / "Changing.zip"
    original_size = 1024 * 1024 + 1
    source.write_bytes(b"A" * original_size)
    real_reader = smt_fingerprint._read_file_chunks
    changed = False

    def changing_reader(path: Path):
        nonlocal changed
        for chunk in real_reader(path):
            yield chunk
            if not changed:
                changed = True
                path.write_bytes(b"B" * original_size)

    monkeypatch.setattr(smt_fingerprint, "_read_file_chunks", changing_reader)

    with pytest.raises(InputChangedError, match="changed while hashing"):
        build_input_manifest(source)


@pytest.mark.parametrize("change", ["add", "delete", "rename", "type"])
def test_source_rebuild_detects_tree_changes(safe_tmp_path: Path, change: str) -> None:
    source = safe_tmp_path / f"Tree-{change}"
    source.mkdir()
    original = source / "A.txt"
    original.write_text("A", encoding="utf-8")
    manifest = build_input_manifest(source)

    if change == "add":
        (source / "B.txt").write_text("B", encoding="utf-8")
    elif change == "delete":
        original.unlink()
    elif change == "rename":
        original.rename(source / "Renamed.txt")
    else:
        original.unlink()
        original.mkdir()

    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_source_rebuild_detects_same_length_overwrite_with_restored_mtime(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Overwrite"
    source.mkdir()
    target = source / "A.txt"
    target.write_bytes(b"AAAA")
    manifest = build_input_manifest(source)
    original_stat = target.stat()

    target.write_bytes(b"BBBB")
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert target.stat().st_size == original_stat.st_size
    assert target.stat().st_mtime_ns == original_stat.st_mtime_ns
    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_verify_imported_directory_rebuilds_manifest(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Source"
    source.mkdir()
    (source / "Empty").mkdir()
    (source / "A.txt").write_text("A", encoding="utf-8")
    manifest = build_input_manifest(source)
    target = safe_tmp_path / ".smt-import.partial"
    shutil.copytree(source, target)

    verify_imported_copy(target, manifest)
    (target / "A.txt").write_text("B", encoding="utf-8")

    with pytest.raises(InputChangedError, match="imported copy changed"):
        verify_imported_copy(target, manifest)


def test_zip_and_suffixless_imported_copy_are_hashed_as_archive(
    safe_tmp_path: Path,
) -> None:
    source = safe_tmp_path / "Example.ZIP"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Data/file.txt", "hello")

    manifest = build_input_manifest(source)
    target = safe_tmp_path / ".smt-import-123.partial"
    shutil.copyfile(source, target)

    assert manifest.source_kind == "zip"
    assert manifest.entries == ()
    assert manifest.digest == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest.source_identity is not None
    verify_imported_copy(target, manifest)

    target.write_bytes(b"not the same archive")
    with pytest.raises(InputChangedError, match="imported copy changed"):
        verify_imported_copy(target, manifest)


def test_7z_archive_is_supported(safe_tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    payload = safe_tmp_path / "payload.txt"
    payload.write_text("hello", encoding="utf-8")
    source = safe_tmp_path / "Example.7z"
    with py7zr.SevenZipFile(source, "w") as archive:
        archive.write(payload, arcname="payload.txt")

    manifest = build_input_manifest(source)

    assert manifest.source_kind == "7z"
    assert manifest.digest == hashlib.sha256(source.read_bytes()).hexdigest()


def test_archive_source_verification_rehashes_content(safe_tmp_path: Path) -> None:
    source = safe_tmp_path / "Example.zip"
    source.write_bytes(b"AAAA")
    manifest = build_input_manifest(source)
    original_stat = source.stat()

    source.write_bytes(b"BBBB")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)


def test_mod_and_workspace_names_are_safe_deterministic_and_utf16_bounded() -> None:
    assert derive_mod_name_candidate(Path('A<B>:C?.zip')) == "A_B__C_"
    example_candidate = derive_mod_name_candidate(Path("Example.7z"))
    assert example_candidate == "Example"
    dragon = "\U0001f409"
    emoji_candidate = derive_mod_name_candidate(Path(f"{dragon * 50}.zip"))
    assert emoji_candidate == dragon * 50
    assert _utf16_units(emoji_candidate) == 100

    digest = "0123456789abcdef"
    example_mod_name = finalize_mod_name(example_candidate, digest, source_kind="7z")
    assert example_mod_name == FinalizedModName(
        source_kind="7z",
        value="Example",
        import_name="Example.7z",
        digest_suffix_applied=False,
        digest_prefix=None,
    )
    occupied = {"example", "Example-01234567", "EXAMPLE-01234567-2"}
    assert choose_workspace_name(example_mod_name, digest, occupied) == "Example-01234567-3"

    emoji_mod_name = finalize_mod_name(emoji_candidate, digest, source_kind="zip")
    first_workspace = choose_workspace_name(emoji_mod_name, digest, ())
    second_workspace = choose_workspace_name(emoji_mod_name, digest, {first_workspace})
    third_workspace = choose_workspace_name(
        emoji_mod_name,
        digest,
        {first_workspace, second_workspace},
    )
    assert emoji_mod_name.digest_suffix_applied
    assert emoji_mod_name.digest_prefix == "01234567"
    assert emoji_mod_name.value.endswith("-01234567")
    assert first_workspace == emoji_mod_name.value
    assert second_workspace.endswith("-01234567-2")
    assert third_workspace.endswith("-01234567-3")
    assert all(
        _utf16_units(name) <= 80
        for name in (
            emoji_mod_name.value,
            first_workspace,
            second_workspace,
            third_workspace,
        )
    )
    assert emoji_mod_name.import_name == f"{emoji_mod_name.value}.zip"
    assert _utf16_units(emoji_mod_name.import_name) <= 80
    assert (Path("mod") / emoji_mod_name.import_name).name == emoji_mod_name.import_name
    with pytest.raises(FrozenInstanceError):
        emoji_mod_name.value = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="finalized"):
        choose_workspace_name(emoji_candidate, digest, ())


def test_exact_80_unit_workspace_name_is_preserved_when_unoccupied() -> None:
    exact_candidate = "A" * 80
    digest = "01234567" + "0" * 56
    exact_mod_name = finalize_mod_name(exact_candidate, digest, source_kind="directory")

    assert exact_mod_name.value == exact_candidate
    assert exact_mod_name.import_name == exact_candidate
    assert not exact_mod_name.digest_suffix_applied
    assert choose_workspace_name(exact_mod_name, digest, ()) == exact_candidate


def test_natural_digest_suffix_is_not_mistaken_for_truncation_metadata() -> None:
    digest = "01234567" + "0" * 56
    natural_name = finalize_mod_name(
        "Example-01234567",
        digest,
        source_kind="directory",
    )

    assert natural_name.value == "Example-01234567"
    assert not natural_name.digest_suffix_applied
    first_collision = choose_workspace_name(natural_name, digest, {"Example-01234567"})
    assert first_collision == "Example-01234567-01234567"
    assert choose_workspace_name(
        natural_name,
        digest,
        {"Example-01234567", first_collision},
    ) == "Example-01234567-01234567-2"


def test_truncated_mod_names_use_digest_to_avoid_workspace_name_aliasing() -> None:
    shared_prefix = "A" * 80
    first_display_name = f"{shared_prefix}X"
    second_display_name = f"{shared_prefix}Y"
    first_candidate = derive_mod_name_candidate(Path(f"{first_display_name}.zip"))
    second_candidate = derive_mod_name_candidate(Path(f"{second_display_name}.zip"))

    assert first_candidate == first_display_name
    assert second_candidate == second_display_name
    first_mod_name = finalize_mod_name(
        first_candidate,
        "11111111" + "0" * 56,
        source_kind="zip",
    )
    second_mod_name = finalize_mod_name(
        second_candidate,
        "22222222" + "0" * 56,
        source_kind="zip",
    )
    first_workspace = choose_workspace_name(first_mod_name, "11111111" + "0" * 56, ())
    second_workspace = choose_workspace_name(second_mod_name, "22222222" + "0" * 56, ())
    assert first_mod_name.value == f"{'A' * 67}-11111111"
    assert second_mod_name.value == f"{'A' * 67}-22222222"
    assert first_mod_name.import_name == f"{first_mod_name.value}.zip"
    assert second_mod_name.import_name == f"{second_mod_name.value}.zip"
    assert first_mod_name.digest_suffix_applied
    assert second_mod_name.digest_suffix_applied
    assert first_workspace == first_mod_name.value
    assert second_workspace == second_mod_name.value
    assert first_mod_name.value != second_mod_name.value
    assert _utf16_units(first_mod_name.value) == 76
    assert _utf16_units(second_mod_name.value) == 76
    assert _utf16_units(first_mod_name.import_name) == 80
    assert _utf16_units(second_mod_name.import_name) == 80


@pytest.mark.parametrize(
    ("source_kind", "extension", "expected_value_units"),
    [("zip", ".zip", 76), ("7z", ".7z", 77)],
)
def test_archive_finalization_reserves_extension_within_80_utf16_units(
    source_kind: str,
    extension: str,
    expected_value_units: int,
) -> None:
    digest = "01234567" + "0" * 56
    finalized = finalize_mod_name(
        "A" * 80,
        digest,
        source_kind=source_kind,  # type: ignore[arg-type]
    )

    assert finalized.source_kind == source_kind
    assert finalized.digest_suffix_applied
    assert finalized.value.endswith("-01234567")
    assert finalized.import_name == f"{finalized.value}{extension}"
    assert _utf16_units(finalized.value) == expected_value_units
    assert _utf16_units(finalized.import_name) == 80

    with pytest.raises(ValueError, match="source kind"):
        finalize_mod_name("Example", digest, source_kind="rar")  # type: ignore[arg-type]


def test_smt_windows_imports_without_loading_win32_bindings() -> None:
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "import smt_windows; "
        "print(smt_windows._win32_bindings.cache_info().currsize)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_known_folder_calls_fail_closed_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smt_windows._win32_bindings.cache_clear()
    monkeypatch.setattr(smt_windows, "_IS_WINDOWS", False)

    with pytest.raises(ManagedProcessEnvironmentError, match="Windows"):
        get_documents_path()
    with pytest.raises(ManagedProcessEnvironmentError, match="Windows"):
        get_local_app_data_path()
    smt_windows._win32_bindings.cache_clear()


@pytest.mark.skipif(os.name != "nt", reason="Windows Known Folder API is required")
def test_known_folder_api_returns_absolute_windows_paths() -> None:
    documents = get_documents_path()
    local_app_data = get_local_app_data_path()

    assert smt_windows.documents_directory() == documents
    assert smt_windows.local_app_data_directory() == local_app_data
    assert documents.is_absolute()
    assert local_app_data.is_absolute()
    assert documents != local_app_data


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_shared_locks_can_coexist_and_preserve_metadata(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "shared.lock"
    with SmtProcessFileLock(
        lock_path,
        "exclusive",
        timeout_seconds=1.0,
        command="run",
    ):
        pass

    metadata = json.loads(lock_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == os.getpid()
    assert metadata["command"] == "run"

    with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
        with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
            assert json.loads(lock_path.read_text(encoding="utf-8")) == metadata


def _run_lock_probe(
    lock_path: Path,
    *,
    exclusive: bool,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    mode = "exclusive" if exclusive else "shared"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "from smt_windows import SmtLockTimeoutError, SmtProcessFileLock; "
        "lock=None; "
        "\ntry:\n"
        f" lock=SmtProcessFileLock({str(lock_path)!r}, {mode!r}, timeout_seconds={timeout!r}); "
        " lock.acquire(); print('acquired')\n"
        "except SmtLockTimeoutError:\n print('timeout'); raise SystemExit(42)\n"
        "finally:\n"
        " if lock is not None: lock.release()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_real_process_shared_and_exclusive_contention(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "cross-process.lock"
    with SmtProcessFileLock(lock_path, "shared", timeout_seconds=1.0):
        shared = _run_lock_probe(lock_path, exclusive=False, timeout=0.5)
        assert shared.returncode == 0, shared.stderr
        assert shared.stdout.strip() == "acquired"

    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        exclusive = _run_lock_probe(lock_path, exclusive=True, timeout=0.1)
        assert exclusive.returncode == 42, exclusive.stderr
        assert exclusive.stdout.strip() == "timeout"
        shared = _run_lock_probe(lock_path, exclusive=False, timeout=0.1)
        assert shared.returncode == 42, shared.stderr
        assert shared.stdout.strip() == "timeout"


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_is_released_by_kernel_when_owner_process_exits(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "abandoned-handle.lock"
    ready_path = safe_tmp_path / "owner.ready"
    code = (
        "import pathlib, sys, time; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        "from smt_windows import SmtProcessFileLock; "
        f"lock=SmtProcessFileLock({str(lock_path)!r}, 'exclusive', timeout_seconds=1); "
        "lock.acquire(); "
        f"pathlib.Path({str(ready_path)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_path.exists()
        owner.kill()
        owner.wait(timeout=5)

        with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
            assert lock_path.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_exclusive_lock_times_out_then_can_be_reacquired(
    safe_tmp_path: Path,
) -> None:
    lock_path = safe_tmp_path / "exclusive.lock"
    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        with pytest.raises(SmtLockTimeoutError):
            with SmtProcessFileLock(
                lock_path,
                "exclusive",
                timeout_seconds=0.1,
            ):
                raise AssertionError("contended exclusive lock must not be acquired")

    assert lock_path.exists()
    with SmtProcessFileLock(lock_path, "exclusive", timeout_seconds=1.0):
        pass


@pytest.mark.skipif(os.name != "nt", reason="LockFileEx is Windows-specific")
def test_lockfileex_different_lock_files_do_not_block_each_other(
    safe_tmp_path: Path,
) -> None:
    first_path = safe_tmp_path / "first.lock"
    second_path = safe_tmp_path / "second.lock"
    with SmtProcessFileLock(first_path, "exclusive", timeout_seconds=1.0):
        second = _run_lock_probe(second_path, exclusive=True, timeout=0.1)
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "acquired"
        assert first_path.exists()
        assert second_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_can_redirect_stdout_and_stderr(
    safe_tmp_path: Path,
) -> None:
    process = start_managed_process(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-line'); print('stderr-line', file=sys.stderr)",
        ],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    stdout, stderr = process.communicate(timeout_seconds=5)

    assert process.returncode == 0
    assert stdout.strip() == "stdout-line"
    assert stderr.strip() == "stderr-line"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_runner_logs_all_output_and_keeps_only_200_tail_lines(
    safe_tmp_path: Path,
) -> None:
    log_path = safe_tmp_path / "smt-cli.log"
    code = "\n".join(
        [
            "import sys",
            "for value in range(250):",
            "    stream = sys.stderr if value % 2 else sys.stdout",
            "    print(f'line-{value}', file=stream, flush=True)",
        ]
    )

    result = ManagedProcess().run(
        [sys.executable, "-c", code],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=5,
        log_path=log_path,
    )

    logged_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert result.exit_code == 0
    assert not result.timed_out
    assert not result.interrupted
    assert len(logged_lines) == 250
    assert len(result.output_tail) == 200
    assert result.output_tail == tuple(logged_lines[-200:])


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_runner_projects_timeout_as_124(
    safe_tmp_path: Path,
) -> None:
    result = ManagedProcess().run(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
        cwd=safe_tmp_path,
        env=os.environ.copy(),
        timeout_seconds=0.2,
        log_path=safe_tmp_path / "timeout.log",
    )

    assert result.exit_code == 124
    assert result.timed_out
    assert not result.interrupted
    assert result.output_tail == ("started",)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_timeout_terminates_descendant_tree(
    safe_tmp_path: Path,
) -> None:
    child_pid_path = safe_tmp_path / "child.pid"
    grandchild_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    process = start_managed_process(
        [sys.executable, "-c", parent_code],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    with pytest.raises(ManagedProcessTimeoutError):
        process.communicate(timeout_seconds=0.5)

    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))
    assert not smt_windows.is_process_running(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects are required")
def test_managed_process_interrupt_closes_descendant_tree(
    safe_tmp_path: Path,
) -> None:
    child_pid_path = safe_tmp_path / "interrupt-child.pid"
    grandchild_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib, subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    process = start_managed_process(
        [sys.executable, "-c", parent_code],
        cwd=safe_tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    deadline = time.monotonic() + 5.0
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_path.exists()
    descendant_pid = int(child_pid_path.read_text(encoding="utf-8"))

    process.interrupt_tree(grace_seconds=0.1)

    assert process.poll() is not None
    assert not smt_windows.is_process_running(descendant_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended processes are required")
def test_job_assignment_failure_never_executes_child_body(
    safe_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = safe_tmp_path / "must-not-exist.txt"
    monkeypatch.setattr(
        smt_windows,
        "_assign_process_to_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("forced assignment failure")
        ),
    )

    with pytest.raises(ManagedProcessEnvironmentError, match="assign"):
        start_managed_process(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            cwd=safe_tmp_path,
        )

    assert not marker.exists()
