from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    "README.md",
    "USER_GUIDE.md",
    "ADVANCED_USER_GUIDE.md",
    "developer_guide.md",
    "docs/agent_adapters.md",
    "docs/agent_compatibility.md",
    "docs/agent_workflow.md",
    "docs/codex_workflow.md",
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


def markdown_section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start + len(heading))
    return text[start:end]


def test_document_set_exists_and_all_local_links_resolve() -> None:
    for relative_path in DOCS:
        path = ROOT / relative_path
        assert path.is_file(), f"missing document: {relative_path}"
        broken = [target for target in local_link_targets(relative_path) if not target.exists()]
        assert not broken, f"broken links in {relative_path}: {broken}"


def test_readme_is_a_short_user_entrypoint() -> None:
    text = read("README.md")
    assert len(text.splitlines()) <= 130
    assert_terms(
        text,
        "Skyrim SE/AE",
        "Fallout 4 Experimental Support",
        r"D:\Fallout4CHS\MyMod",
        "翻译 mod",
        "out/<ModName>/汉化产出/",
        "USER_GUIDE.md",
        "ADVANCED_USER_GUIDE.md",
        "developer_guide.md",
        "fallout4_experimental_support.md",
        "STRINGS",
        "DLSTRINGS",
        "ILSTRINGS",
        "PEX Apply",
        "BA2",
        "不重打包",
        "Codex",
        "opencode",
        "Claude Code",
    )
    assert "localized" not in text
    assert "non-localized" not in text
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
    )
    assert "localized" not in text
    assert "non-localized" not in text
    for internal_detail in ("fixture", "ci_validate_repo.py", "workflow_policy.json"):
        assert internal_detail not in text


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
        "capabilities.archive.ba2.level",
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
        "mutagen-bethesda-plugin",
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
        "mutagen-bethesda-plugin",
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
        "capabilities.archive.ba2.level",
        "SWF",
        "GFX",
        "DLL",
        "EXE",
        "合成 fixture",
        "真实游戏认证",
    )
    for tutorial_detail in ("marketplace add", "init_workspace.py", "翻译 mod"):
        assert tutorial_detail not in text


def test_docs_match_profile_and_fixed_pex_strict_gate() -> None:
    skyrim = json.loads(read("config/game_profiles/skyrim-se.json"))
    fallout4 = json.loads(read("config/game_profiles/fallout4.json"))
    assert skyrim["capabilities"]["archive.bsa"]["level"] == "read_only"
    assert fallout4["capabilities"]["archive.ba2"]["level"] == "read_only"
    assert fallout4["capabilities"]["pex"]["level"] == "experimental_write"

    advanced = read("ADVANCED_USER_GUIDE.md")
    reference = read("docs/fallout4_experimental_support.md")
    developer = read("developer_guide.md")
    strict_gate_source = read("scripts/run_non_gui_qa_gates.py")
    used_capability_source = read("scripts/used_capabilities.py")
    assert 'row.get("strict_complete_allowed") is not True' in strict_gate_source
    assert '"strict_complete_allowed": operation_record["strict_complete_allowed"]' in used_capability_source
    assert "strict completion" in advanced
    assert "不能作为正式汉化交付" in advanced
    assert "capabilities.archive.ba2.level" in advanced
    assert "strict completion" in reference
    assert "固定阻断" in reference
    assert "capabilities.archive.ba2.level" in reference
    assert "没有可由用户补交的证据" in advanced
    assert "没有可提交的额外证据" in reference
    assert "固定判定为不可放行" in developer
    assert "补齐 opt-in 与 strict 证据" not in advanced


def test_agent_docs_match_non_gui_capabilities() -> None:
    capabilities = json.loads(read("config/agent_capabilities.example.json"))["agents"]
    for name in ("opencode", "claude-code"):
        assert capabilities[name]["supports_controller_mode"] is True
        assert capabilities[name]["supports_gui_automation"] is False
        assert capabilities[name]["supports_computer_use"] is False
        assert capabilities[name]["gui_handoff_target"] == "codex"
    advanced = read("ADVANCED_USER_GUIDE.md")
    assert "非 GUI 顶层主控" in advanced
    assert "它们不是子智能体 worker" in advanced
    assert "必须 blocked" in advanced


def test_agent_docs_have_distinct_responsibilities() -> None:
    index = read("docs/agent_adapters.md")
    compatibility = read("docs/agent_compatibility.md")
    non_gui_workflow = read("docs/agent_workflow.md")
    codex_workflow = read("docs/codex_workflow.md")

    assert_terms(index, "Agent 入口索引", "Agent Compatibility", "Non-GUI Agent Workflow", "Codex 接手指南")
    for implementation_detail in ("init_opencode.py", "write_agent_handoff.py --check-freshness", "claim_workflow_task.py"):
        assert implementation_detail not in index

    assert_terms(compatibility, "支持矩阵", "GUI 边界", "主控与子 Agent", "Codex 性能边界")
    for workflow_detail in ("## 接手顺序", "--check-freshness", "claim_workflow_task.py"):
        assert workflow_detail not in compatibility

    assert_terms(non_gui_workflow, "opencode 和 Claude Code", "qa/agent_handoff.json", "handoff_target=codex")
    assert "/plugin marketplace add" not in non_gui_workflow
    assert "init_opencode.py" not in non_gui_workflow

    assert codex_workflow.startswith("# Codex 接手指南")
    assert_terms(
        codex_workflow,
        "Non-GUI Agent Workflow",
        "完整增强版",
        "优先读取 `qa/codex_handoff.json`",
        "Computer Use",
        "gui:desktop",
        "Codex 原生能力与插件辅助",
        "agentops:validate",
        "data-analytics:build-report",
    )
    for nonexistent_agentops_skill in ("agentops:validation", "agentops:trace", "agentops:harvest"):
        assert nonexistent_agentops_skill not in codex_workflow

    assert "## 共同边界" not in index
    assert len(index.splitlines()) <= 20
    assert not (ROOT / "docs/codex_plugin_adapter.md").exists()


def test_decoder_first_doc_is_an_architecture_overview() -> None:
    decoder = read("docs/decoder_first_workflow.md")
    assert_terms(
        decoder,
        "本页只维护架构、顺序和文档分工",
        "Tool Adapter",
        "PEX Visible Strings Writeback",
        "Translation Proofreading Workflow",
        "Final Mod Output",
        "BA2 inventory/materialization 都由 `ba2-archive-audit` 编排",
    )
    for duplicated_detail in (
        '"LexTranslatorPath":',
        "python .\\scripts\\invoke_bsa_file_extractor_safe.py",
        "python .\\scripts\\invoke_ba2_extractor_safe.py",
        "python .\\scripts\\verify_plugin_output.py",
        "## PEX 的无 GUI 目标",
        "## final_mod 文本结构门槛",
        "## BSA loose override 交付策略",
    ):
        assert duplicated_detail not in decoder


def test_status_refresh_does_not_implicitly_run_strict_qa() -> None:
    codex_workflow = read("docs/codex_workflow.md")
    refresh = markdown_section(codex_workflow, "## 状态刷新入口", "## 进度卡和 Trace")
    assert "缺失、过期或互相矛盾" in refresh
    assert "python scripts/test_workflow_health.py\n" in refresh
    assert "--run-strict-gate" not in refresh.split("普通接手", 1)[0]
    assert "用户明确要求运行严格 QA" in refresh

    usage_skill = read(".codex/skills/skyrim-mod-chs-usage/SKILL.md")
    assert "test_workflow_health.py --run-strict-gate" not in usage_skill
    assert "普通状态刷新不得附加 `--run-strict-gate`" in usage_skill


def test_strict_qa_reference_uses_validate_final_mod_cli_contract() -> None:
    contract = read("skills/qa-validation/references/strict-qa-contract.md")
    assert "validate_final_mod.py --final-mod-dir out/<ModName>/汉化产出/final_mod" in contract
    assert "validate_final_mod.py --mod-name" not in contract

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_final_mod.py"), "--help"],
        check=True,
        capture_output=True,
    )
    assert b"--final-mod-dir" in result.stdout
    assert b"--mod-name" not in result.stdout


def test_maintenance_smoke_path_is_repeatable() -> None:
    maintenance = read(".codex/skills/skyrim-mod-chs-maintenance/SKILL.md")
    assert "[guid]::NewGuid()" in maintenance
    assert "python scripts\\init_workspace.py $smoke --game skyrim-se" in maintenance
    assert "D:\\SkyrimCHS\\maintenance-smoke" not in maintenance


def test_non_gui_workflow_binds_workspace_and_replays_progress_card() -> None:
    workflow = read("docs/agent_workflow.md")
    assert_terms(
        workflow,
        "如果用户只问当前进度",
        ".workflow/progress_card.md",
        "SKYRIM_CHS_PLUGIN_ROOT",
        "SKYRIM_CHS_WORKSPACE_ROOT",
        "<WorkspaceRoot>",
        "[SMT 进度]",
        "[SMT 阻断]",
        "[SMT 完成]",
    )
    assert 'python "$env:SKYRIM_CHS_PLUGIN_ROOT\\scripts\\write_agent_handoff.py" --agent <opencode|claude-code> --check-freshness' in workflow


def test_skill_architecture_lists_every_runtime_skill() -> None:
    architecture = read("docs/skill_architecture.md")
    runtime_skills = sorted(
        path.parent.name
        for path in (ROOT / "skills").glob("*/SKILL.md")
        if path.is_file()
    )
    assert f"当前工程共有 {len(runtime_skills)} 个运行 Skill" in architecture
    missing = [name for name in runtime_skills if f"`{name}`" not in architecture]
    assert not missing, f"runtime skills missing from skill architecture: {missing}"
    assert_terms(
        architecture,
        "| 工作区与工具准备 | `workspace-tool-setup` |",
        "“初始化工作区”“选择游戏”“自动准备工具”",
        "| 新建工作区或准备工具 |",
    )


def test_entry_skill_delegates_tool_setup_implementation() -> None:
    entry = read("skills/skyrim-mod-chs-translation/SKILL.md")
    assert_terms(entry, "plugin source", "initialized workspace", "workspace-tool-setup", "--game skyrim-se", "--game fallout4")
    for setup_implementation in ("dotnet-install.ps1", "BSAFileExtractor", "Champollion", "uv venv"):
        assert setup_implementation not in entry


def test_adapter_docs_are_agent_execution_contracts() -> None:
    opencode = read("docs/opencode_adapter.md")
    claude = read("docs/claude_code_adapter.md")

    for text in (opencode, claude):
        assert_terms(text, "## 触发条件", "## 前置检查", "## 验证证据", "## 停止条件")
    assert_terms(opencode, "## 初始化动作", "## 配置合同", "## 断点恢复")
    assert_terms(claude, "## 安装动作", "## 接手上下文")
    assert "## 一键初始化和启动" not in opencode
    assert "Marketplace 安装：" not in claude


def test_gui_docs_are_agent_execution_contracts() -> None:
    lextranslator = read("docs/lextranslator_workflow.md")
    xtranslator = read("docs/xtranslator_workflow.md")

    for text in (lextranslator, xtranslator):
        assert_terms(
            text,
            "## 触发条件",
            "## 必读输入",
            "## 执行动作",
            "## 停止条件",
            "Computer Use",
            "blocked",
        )
        assert "建议先用小 Mod 测试" not in text
    assert_terms(lextranslator, "translation-task-router", "## 输出与证据", "translated/tool_outputs/<ModName>/")
    assert_terms(xtranslator, "Router", "## 输出与 QA", "verify_plugin_output.py", "verify_pex_output.py")


def test_workspace_game_selection_is_explicit_at_cli_and_agent_entries() -> None:
    readme = read("README.md")
    assert_terms(
        readme,
        r"D:\SkyrimCHS\MyMod",
        r"D:\Fallout4CHS\MyMod",
        "创建一个",
        "汉化工作区",
        "Agent 会先询问并等待确认",
    )

    user_guide = read("USER_GUIDE.md")
    assert "--game skyrim-se" in user_guide
    assert "--game fallout4" in user_guide
    assert "二次确认" in user_guide

    for skill_path in (
        "skills/skyrim-mod-chs-translation/SKILL.md",
        "skills/workspace-tool-setup/SKILL.md",
        ".codex/skills/skyrim-mod-chs-usage/SKILL.md",
    ):
        text = read(skill_path)
        assert "Skyrim SE/AE 还是 Fallout 4" in text
        assert "自然语言" in text
        assert "--game skyrim-se" in text
        assert "--game fallout4" in text

    for prompt_path in ("agents/opencode/prompt.md", "agents/claude-code/prompt.md"):
        text = read(prompt_path)
        assert "Skyrim SE/AE 还是 Fallout 4" in text
        assert "自然语言" in text


def test_agent_docs_and_runtime_skills_are_profile_aware_for_both_games() -> None:
    generic_docs = {
        "docs/codex_workflow.md": "Bethesda Mod 简体中文汉化",
        "docs/decoder_first_workflow.md": "当前 Game Profile 的 Data 根",
        "docs/final_mod_output.md": "当前 Game Profile 的 Data 根结构",
        "docs/mod_sandbox_rules.md": "Skyrim SE/AE 与 Fallout 4",
        "docs/skill_architecture.md": "Bethesda Mod 简体中文汉化工作流源仓库",
        "docs/tool_adapter.md": "当前 Game Profile 对应的真实游戏目录",
        "docs/translation_rules.md": "根据当前 Game Profile 和作品语境",
        "docs/translation_proofreading_workflow.md": "当前 Game Profile 对应游戏语境",
        "docs/effect_regression_workflow.md": "任何受支持游戏的真实目录",
    }
    for path, expected in generic_docs.items():
        assert expected in read(path)

    final_skill = read("skills/final-mod-assembly/SKILL.md")
    for expected in ("BSA/BA2", "extraction receipt", "entry hash", "不重打包 BA2"):
        assert expected in final_skill

    router = read("skills/translation-task-router/SKILL.md")
    assert "`capabilities.string_tables` 不满足 read" in router
    assert "`pex` capability 不满足 read" in router
    assert "未声明或未实现的能力必须 fail closed" in router

    esp_skill = read("skills/esp-esm-esl-translation/SKILL.md")
    assert "禁止把未知游戏或未知 adapter 归入 Skyrim 分支" in esp_skill
    pex_skill = read("skills/pex-visible-strings-translation/SKILL.md")
    assert "pex_category=none" in pex_skill


def test_ba2_and_gui_docs_follow_profile_capabilities() -> None:
    ba2_skill = read("skills/ba2-archive-audit/SKILL.md")
    assert "according to the current Game Profile" in ba2_skill
    assert 'can_materialize_archive(".ba2")' in ba2_skill
    assert "current Skyrim profile is inventory-only" in ba2_skill
    assert "workspace-local Fallout 4 `.ba2` archives" not in ba2_skill

    bsa_skill = read("skills/bsa-archive-audit/SKILL.md")
    assert "Route every BA2 request" in bsa_skill
    assert "generic read-only BSA/BA2 inventory" not in bsa_skill
    assert "`mod/**/*.ba2`" not in bsa_skill

    decoder_workflow = read("docs/decoder_first_workflow.md")
    assert "BA2 inventory/materialization 都由 `ba2-archive-audit` 编排" in decoder_workflow

    ba2_interface = read("skills/ba2-archive-audit/agents/openai.yaml")
    bsa_interface = read("skills/bsa-archive-audit/agents/openai.yaml")
    for interface in (ba2_interface, bsa_interface):
        assert "Bethesda" in interface
        assert "current Game Profile" in interface
        assert "Fallout 4" not in interface
        assert "Skyrim" not in interface

    recovery_skill = read("skills/workflow-agent-orchestration/SKILL.md")
    assert "Controlled BA2 materialization is not a blanket stop condition" in recovery_skill
    assert "BA2 extraction/writeback" not in recovery_skill
    assert "all BA2 writeback/repacking" in recovery_skill

    gui_rules = read("docs/gui_automation_rules.md")
    assert "decoder 不可用本身不授权 GUI" in gui_rules
    assert "Fallout 4 localized plugin/STRINGS 固定 blocked" in gui_rules

    lextranslator = read("docs/lextranslator_workflow.md")
    xtranslator = read("docs/xtranslator_workflow.md")
    for text in (lextranslator, xtranslator):
        assert "通用说明不构成 Fallout 4 GUI 认证" in text
        assert "Fallout 4 localized plugin/STRINGS" in text
    assert "备份原插件" not in xtranslator


def test_profile_contract_and_examples_use_format_level_capabilities() -> None:
    fallout_contract = read("docs/fallout4_experimental_support.md")
    assert "capabilities.archive.bsa.level" in fallout_contract
    assert "capabilities.archive.ba2.level" in fallout_contract
    assert "资源 capability 是执行与严格 QA 的唯一能力来源" in fallout_contract
    for removed_field in (
        "archive_materialization_extensions",
        "archive_repack_extensions",
        "archive_materialization_enabled",
        "archive_allow_repack",
    ):
        assert removed_field not in fallout_contract

    final_output = read("docs/final_mod_output.md")
    assert "SKSE/ 或 F4SE/" not in final_output
    assert "不得为了匹配示例主动创建空目录" in final_output

    translation_rules = read("docs/translation_rules.md")
    assert "Skyrim 保留奇幻感" in translation_rules
    assert "Fallout 4 保留废土、科技和复古未来语境" in translation_rules


def test_archive_and_ci_docs_match_current_capabilities() -> None:
    final_output = read("docs/final_mod_output.md")
    assert_terms(final_output, "`.zip` 和 `.7z`", "Python `py7zr`", "DecoderTools.Archive7zPath")
    assert "`.rar` 默认只生成提取建议" in final_output
    assert "`.rar` 和 `.7z` 默认只生成提取建议" not in final_output

    regression = read("docs/effect_regression_workflow.md")
    assert_terms(
        regression,
        "scripts/test_skill_effects.py",
        "scripts/test_game_profile_regressions.py",
        "scripts/test_glossary_binary_formats.py",
        "scripts/test_fallout4_routing_regressions.py",
        "windows-fallout4-adapters",
        "windows-fallout4-workflow",
        "已经受跟踪的历史合同测试仍由 Git 和 CI 保留",
    )


def test_bsa_materialization_docs_require_adapter_result_lineage() -> None:
    skill = read("skills/bsa-archive-audit/SKILL.md")
    decoder = read("docs/decoder_first_workflow.md")
    adapter = read("docs/tool_adapter.md")

    for text in (skill, decoder, adapter):
        assert "--adapter-result-path" in text
        assert "AdapterResult" in text
    assert "wrapper writes the extraction-backed manifest" in skill
    assert "cannot replace it" in skill
    assert "不能单独建立 strict completion 所需的 AdapterResult lineage" in decoder
    assert "单独运行 `new_archive_audit_manifest.py` 不能建立严格门禁" in adapter


def test_checkpoint_docs_distinguish_changes_from_bounded_snapshot_blockers() -> None:
    workflow = read("docs/agent_workflow.md")
    policy = read("skills/workflow-policy-and-state/SKILL.md")
    recovery = read("skills/workflow-agent-orchestration/SKILL.md")

    assert_terms(
        workflow,
        "reasons[]",
        "snapshot_changed",
        "checkpoint_snapshot_incomplete",
        "evidence_ref_limit_exceeded",
        "checkpoint_read_budget_exhausted",
        "64 个 evidence refs",
        "32 MiB",
        "不得反复刷新",
    )
    for text in (policy, recovery):
        assert "reasons[]" in text
        assert "checkpoint_snapshot_incomplete" in text or "incomplete snapshots" in text
        assert "refresh" in text
        assert "blocker" in text


def test_pex_docs_resolve_registry_before_using_builtin_mutagen_example() -> None:
    skill = read("skills/pex-visible-strings-translation/SKILL.md")
    writeback = read("docs/pex_visible_strings_writeback.md")
    adapter = read("docs/tool_adapter.md")

    for text in (skill, writeback, adapter):
        assert "Adapter Registry" in text or "Registry" in text
        assert "mutagen-pex" in text
    assert "当前内置 mutagen-pex 示例" in skill
    assert "当前内置 mutagen-pex 示例流程" in writeback
    assert "不得把固定 Mutagen 脚本当作所有游戏的通用入口" in skill
    assert "不是跨游戏硬编码合同" in adapter


def test_non_gui_adapter_docs_do_not_declare_a_default_game() -> None:
    for path in ("agents/opencode/README.md", "agents/claude-code/README.md"):
        text = read(path)
        assert "新工作区没有默认游戏" in text
        assert "默认完整流程" not in text

    for path in ("agents/opencode/adapter.json", "agents/claude-code/adapter.json"):
        text = read(path)
        assert "new workspaces have no default game" in text
        assert "default Skyrim" not in text
        json.loads(text)


def test_fallout4_contract_documents_authoritative_capabilities() -> None:
    profile = json.loads(read("config/game_profiles/fallout4.json"))
    contract = read("docs/fallout4_experimental_support.md")
    expected = {
        "capabilities.plugin_text.level": profile["capabilities"]["plugin_text"]["level"],
        "capabilities.plugin_text.adapter": profile["capabilities"]["plugin_text"]["adapter"],
        "capabilities.pex.level": profile["capabilities"]["pex"]["level"],
        "capabilities.pex.adapter": profile["capabilities"]["pex"]["adapter"],
        "capabilities.archive.bsa.level": profile["capabilities"]["archive.bsa"]["level"],
        "capabilities.archive.ba2.level": profile["capabilities"]["archive.ba2"]["level"],
        "capabilities.loose_text.level": profile["capabilities"]["loose_text"]["level"],
        "capabilities.string_tables.level": profile["capabilities"]["string_tables"]["level"],
    }
    for capability_path, value in expected.items():
        assert f"`{capability_path}`" in contract
        assert f"`{value}`" in contract
    assert "capabilities.plugin_text.options.localized_plugin_policy=block" in contract
    assert "不读取、不派生也不传播旧顶层能力字段" in contract


def test_task7_fallout4_resource_and_delivery_contracts_are_documented() -> None:
    reference = read("docs/fallout4_experimental_support.md")
    assert_terms(
        reference,
        "Materials/*.bgsm",
        "Materials/*.bgem",
        "Meshes/",
        "Textures/",
        "Sound/",
        "Music/",
        "Video/",
        "Vis/",
        "Seq/",
        "MCM 是 container",
        "JSON、INI、TOML、TXT",
        "F4SE DLL",
        "INI/TOML 整行注释",
        "key/value",
        "Interface/translations/*.txt",
        "原相对路径和原文件名",
        "final_mod 是完整 Mod",
    )

    translation_rules = read("docs/translation_rules.md")
    assert_terms(
        translation_rules,
        "MCM 是 container",
        "JSON、INI、TOML、TXT",
        "F4SE",
        "玩家可见 value",
        "key、路径、协议值和内部标识",
        "SWF/GFX",
        "Interface/translations/*.txt",
    )

    final_output = read("docs/final_mod_output.md")
    assert_terms(
        final_output,
        "Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq",
        "只能从工作区 `mod/` 原样复制",
        "source SHA256 与 final SHA256 必须相同",
        "`tool_outputs` 只允许当前 Game Profile 明确开放写回的插件或 PEX",
        "原相对路径和原文件名",
        "final_mod 是完整 Mod",
    )


def test_readme_keeps_task7_support_summary_short_and_user_facing() -> None:
    readme = read("README.md")
    assert_terms(
        readme,
        "材质、网格、纹理、音频和视频资源",
        "原样保留",
        "SWF、GFX、DLL、EXE",
    )
    for implementation_detail in (
        "BGSM",
        "BGEM",
        "trait_level_caps",
        "source SHA256",
        "adapter_contract_version",
    ):
        assert implementation_detail not in readme


def test_developer_validation_lists_capability_architecture_regressions() -> None:
    developer = read("developer_guide.md")
    for script_name in (
        "test_capability_resolver.py",
        "test_adapter_registry.py",
        "test_plugin_capability_adapter.py",
        "test_archive_capabilities.py",
        "test_bsa_loose_override.py",
        "test_used_capabilities.py",
        "test_agent_handoff_checkpoint_regressions.py",
    ):
        assert script_name in developer
    assert "capability/Registry" in developer
    assert "BSA capability evidence" in developer
