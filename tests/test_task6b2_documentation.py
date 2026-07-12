from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    "README.md",
    "USER_GUIDE.md",
    "ADVANCED_USER_GUIDE.md",
    "developer_guide.md",
    "docs/fallout4_experimental_support.md",
)
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def local_link_targets(relative_path: str) -> list[Path]:
    source = ROOT / relative_path
    targets: list[Path] = []
    for match in LINK_RE.finditer(read(relative_path)):
        raw_target = match.group(1).strip().strip("<>")
        if raw_target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path_part = unquote(raw_target.split("#", 1)[0])
        if path_part:
            targets.append((source.parent / path_part).resolve(strict=False))
    return targets


def assert_terms(text: str, *terms: str) -> None:
    missing = [term for term in terms if term not in text]
    assert not missing, f"missing terms: {missing}"


def test_document_set_exists_and_all_local_links_resolve() -> None:
    for relative_path in DOCS:
        path = ROOT / relative_path
        assert path.is_file(), f"missing document: {relative_path}"
        broken = [target for target in local_link_targets(relative_path) if not target.exists()]
        assert not broken, f"broken links in {relative_path}: {broken}"


def test_readme_is_a_short_user_entrypoint() -> None:
    text = read("README.md")
    assert_terms(
        text,
        "Skyrim SE/AE",
        "Fallout 4 Experimental Support",
        "--game fallout4",
        "翻译 mod",
        "out/<ModName>/汉化产出/",
        "USER_GUIDE.md",
        "ADVANCED_USER_GUIDE.md",
        "developer_guide.md",
        "fallout4_experimental_support.md",
        "localized",
        "STRINGS",
        "PEX Apply",
        "BA2",
        "不重打包",
        "Codex",
        "opencode",
        "Claude Code",
    )
    for internal_detail in ("ci_validate_repo.py", "workflow_policy.json", "--launch-mode"):
        assert internal_detail not in text


def test_user_guide_owns_the_daily_workflow_and_game_selection() -> None:
    text = read("USER_GUIDE.md")
    assert_terms(
        text,
        "--game fallout4",
        ".skyrim-chs-workspace.json",
        "不按 Mod 名",
        "翻译 mod",
        "继续汉化",
        "[SMT 进度]",
        "final_mod/",
        "_CHS.zip",
        "人工",
        "Classic Holstered Weapons - v1.09-46101-1-09-1779912557",
        "合成 fixture",
        "不代表该 Mod 的真实二进制已测试",
    )
    assert "该 Mod 的真实二进制已经通过测试" not in text


def test_advanced_guide_owns_tools_boundaries_reports_and_recovery() -> None:
    text = read("ADVANCED_USER_GUIDE.md")
    assert_terms(
        text,
        "DecoderTools.Ba2ExtractorPath",
        "opencode",
        "Claude Code",
        "Codex",
        "localized",
        "STRINGS",
        "PEX Export",
        "PEX Apply",
        "strict",
        "BA2",
        "loose override",
        "不重打包",
        "SWF",
        "GFX",
        "DLL",
        "EXE",
        "game_id",
        "mismatch",
        "恢复",
        "fallout4_experimental_support.md",
    )
    assert "发布工程源码包" not in text


def test_developer_guide_owns_architecture_tests_and_release_maintenance() -> None:
    text = read("developer_guide.md")
    assert_terms(
        text,
        "Game Profile",
        "GameContext",
        "skyrim-mutagen",
        "fallout4-mutagen",
        "metadata",
        "schema",
        "workflow_state.json",
        "resource_locks",
        "子智能体",
        "fixture",
        "CI",
        "扩展新游戏",
        "版本",
        "发布",
        "fallout4_experimental_support.md",
    )


def test_fallout4_reference_is_an_audit_contract_not_a_user_tutorial() -> None:
    text = read("docs/fallout4_experimental_support.md")
    assert_terms(
        text,
        "Fallout 4 Experimental Support",
        ".skyrim-chs-workspace.json",
        "fallout4-mutagen",
        "Fallout4Mod",
        "localized",
        "STRINGS",
        "PEX Export",
        "PEX Apply",
        "strict",
        "BA2",
        "receipt",
        "manifest",
        "loose override",
        "allow_repack=false",
        "SWF",
        "GFX",
        "DLL",
        "EXE",
        "合成 fixture",
        "真实游戏认证",
    )
    for tutorial_detail in ("marketplace add", "init_workspace.py", "翻译 mod"):
        assert tutorial_detail not in text
