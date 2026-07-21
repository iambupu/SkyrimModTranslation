from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from list_agent_skills import skill_rows  # noqa: E402
from run_effect_regression import count_skill_frontmatter  # noqa: E402


@dataclass(frozen=True)
class SkillEffectCase:
    path: str
    prompt: str
    trigger_anchors: tuple[str, ...]
    confusion_skill: str
    probe_script: str

    @property
    def name(self) -> str:
        return Path(self.path).parent.name


CASES = (
    SkillEffectCase(
        "skills/ba2-archive-audit/SKILL.md",
        "给这个 Fallout 4 BA2 做 inventory，并验证解包 receipt 和 hash。",
        ("BA2", "receipt/manifest/hash"),
        "bsa-archive-audit",
        "scripts/invoke_ba2_extractor_safe.py",
    ),
    SkillEffectCase(
        "skills/bsa-archive-audit/SKILL.md",
        "检查这个 BSA，并在允许时通过 BSAFileExtractor 安全解包。",
        ("BSA", "BSAFileExtractor"),
        "ba2-archive-audit",
        "scripts/invoke_bsa_file_extractor_safe.py",
    ),
    SkillEffectCase(
        "skills/bethesda-string-table-translation/SKILL.md",
        "导出 STRINGS/DLSTRINGS/ILSTRINGS，并按 string ID 受控写回和验证。",
        ("STRINGS", "string ID"),
        "text-resource-translation",
        "scripts/invoke_bethesda_string_table_tool.py",
    ),
    SkillEffectCase(
        "skills/esp-esm-esl-translation/SKILL.md",
        "导出这个 Fallout 4 ESP 的可翻译字段，保留 FormID 和 EditorID。",
        ("ESP/ESM/ESL", "FormID"),
        "pex-visible-strings-translation",
        "scripts/export_esp_strings.py",
    ),
    SkillEffectCase(
        "skills/final-mod-assembly/SKILL.md",
        "组装 final_mod 并生成带 provenance 的 _CHS.zip。",
        ("final_mod", "_CHS.zip"),
        "qa-validation",
        "scripts/build_final_mod.py",
    ),
    SkillEffectCase(
        "skills/glossary-management/SKILL.md",
        "用当前游戏的 SST 和 EET 词典统一角色译名。",
        ("SST", "EET"),
        "text-resource-translation",
        "scripts/build_external_glossary_matches.py",
    ),
    SkillEffectCase(
        "skills/lextranslator-gui-automation/SKILL.md",
        "路由已经批准 LexTranslator GUI 后备，请保存到 tool_outputs。",
        ("LexTranslator", "tool_outputs"),
        "xtranslator-gui-automation",
        "scripts/invoke_lextranslator_gui.py",
    ),
    SkillEffectCase(
        "skills/mcm-translation/SKILL.md",
        "翻译 MCM Helper 的菜单页面、选项和帮助文本。",
        ("MCM Helper", "菜单页面"),
        "text-resource-translation",
        "scripts/extract_mcm_text.py",
    ),
    SkillEffectCase(
        "skills/mod-input-preparation/SKILL.md",
        "扫描 mod 目录，解压 ZIP/7Z 并生成输入清单。",
        ("扫描 mod", "解压 ZIP/7Z"),
        "translation-task-router",
        "scripts/detect_mod_files.py",
    ),
    SkillEffectCase(
        "skills/pex-visible-strings-translation/SKILL.md",
        "导出 Papyrus PEX 中 MessageBox 的可见字符串。",
        ("Papyrus", "MessageBox"),
        "esp-esm-esl-translation",
        "scripts/invoke_mutagen_pex_string_tool.py",
    ),
    SkillEffectCase(
        "skills/qa-validation/SKILL.md",
        "运行严格门禁，检查 final_mod 的 hash 和 provenance。",
        ("严格门禁", "provenance"),
        "final-mod-assembly",
        "scripts/run_non_gui_qa_gates.py",
    ),
    SkillEffectCase(
        "skills/skyrim-mod-chs-translation/SKILL.md",
        "帮我初始化 Fallout 4 工作区，然后开始汉化 mod。",
        ("初始化工作区", "汉化 mod"),
        "workspace-tool-setup",
        "",
    ),
    SkillEffectCase(
        "skills/skyrim-mod-translation-orchestrator/SKILL.md",
        "入口已经完成分类，请按状态机推进运行期编排。",
        ("状态机推进", "运行期编排"),
        "workflow-policy-and-state",
        "scripts/run_non_gui_translation_workflow.py",
    ),
    SkillEffectCase(
        "skills/text-resource-translation/SKILL.md",
        "翻译 Interface 下的 JSON/XML/CSV/TXT，同时保留 key 和结构。",
        ("Interface 翻译", "JSON/XML"),
        "mcm-translation",
        "scripts/normalize_export.py",
    ),
    SkillEffectCase(
        "skills/translation-task-router/SKILL.md",
        "判断这个 BA2 文件的风险等级，并告诉我该用哪个工具。",
        ("风险等级", "该用哪个工具"),
        "mod-input-preparation",
        "scripts/route_translation_task.py",
    ),
    SkillEffectCase(
        "skills/workflow-agent-orchestration/SKILL.md",
        "流程进入 qa_failed，请记录尝试并安全恢复 QA。",
        ("qa_failed", "恢复 QA"),
        "workflow-subagent-orchestration",
        "scripts/resume_workflow.py",
    ),
    SkillEffectCase(
        "skills/workflow-policy-and-state/SKILL.md",
        "入口已识别状态查询，请读取 workflow_state 并判断允许动作。",
        ("读取 workflow_state", "判断允许动作"),
        "workflow-agent-orchestration",
        "scripts/write_workflow_state.py",
    ),
    SkillEffectCase(
        "skills/workflow-subagent-orchestration/SKILL.md",
        "把 workflow_tasks 中可并发的 resource lane 分配给子智能体。",
        ("子智能体", "workflow_tasks"),
        "workflow-agent-orchestration",
        "scripts/claim_workflow_task.py",
    ),
    SkillEffectCase(
        "skills/workspace-tool-setup/SKILL.md",
        "入口已确认 Skyrim SE 和目标路径，请执行自动准备工具并生成 tools.local.json。",
        ("入口已确认", "自动准备工具"),
        "skyrim-mod-chs-translation",
        "scripts/init_workspace.py",
    ),
    SkillEffectCase(
        "skills/xtranslator-gui-automation/SKILL.md",
        "路由已经批准 xTranslator GUI 后备，请检查插件导出并做精修。",
        ("xTranslator", "GUI 后备"),
        "lextranslator-gui-automation",
        "scripts/invoke_xtranslator.py",
    ),
    SkillEffectCase(
        ".codex/skills/skyrim-mod-chs-install/SKILL.md",
        "重新安装 Codex 插件并刷新本地 marketplace。",
        ("重新安装插件", "marketplace"),
        "skyrim-mod-chs-maintenance",
        "scripts/install_codex_plugin.py",
    ),
    SkillEffectCase(
        ".codex/skills/skyrim-mod-chs-maintenance/SKILL.md",
        "优化 Skill 触发并给仓库跑 smoke test。",
        ("优化 Skill 触发", "smoke test"),
        "skyrim-mod-chs-usage",
        "scripts/init_workspace.py",
    ),
    SkillEffectCase(
        ".codex/skills/skyrim-mod-chs-usage/SKILL.md",
        "这个插件怎么使用，mod 放哪里，怎么看进度卡？",
        ("怎么使用", "mod 放哪里"),
        "skyrim-mod-chs-maintenance",
        "scripts/init_workspace.py",
    ),
)

CASE_BY_NAME = {case.name: case for case in CASES}
SCRIPT_REFERENCE_RE = re.compile(r"scripts/[A-Za-z0-9_.\-/]+\.(?:py|ps1)")
ENTRY_ONLY_TRIGGERS = (
    "初始化工作区",
    "选择游戏",
    "开始/继续汉化",
    "进度卡",
    "生成 final_mod",
    "blocked 怎么办",
)
INTERNAL_SKILL_PRECONDITIONS = {
    "workspace-tool-setup": "入口已确认",
    "workflow-policy-and-state": "入口已识别状态查询",
    "skyrim-mod-translation-orchestrator": "入口已完成分类",
}


def read_skill(case: SkillEffectCase) -> str:
    return (ROOT / case.path).read_text(encoding="utf-8-sig")


def frontmatter(text: str) -> dict[str, str]:
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}
    result: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


class SkillTriggerEffectTests(unittest.TestCase):
    def runtime_descriptions(self) -> dict[str, str]:
        return {
            case.name: frontmatter(read_skill(case)).get("description", "")
            for case in CASES
            if case.path.startswith("skills/")
        }

    def test_effect_cases_cover_every_tracked_skill(self) -> None:
        runtime = {
            path.parent.name
            for path in (ROOT / "skills").glob("*/SKILL.md")
        }
        tracked_meta = {
            path.parent.name
            for path in (ROOT / ".codex" / "skills").glob("skyrim-mod-chs-*/SKILL.md")
        }
        self.assertEqual(set(CASE_BY_NAME), runtime | tracked_meta)
        self.assertEqual(len(CASES), len(CASE_BY_NAME), "duplicate Skill effect case")

    def test_natural_language_trigger_anchors_are_in_metadata(self) -> None:
        for case in CASES:
            with self.subTest(skill=case.name):
                metadata = frontmatter(read_skill(case))
                self.assertEqual(metadata.get("name"), case.name)
                description = metadata.get("description", "")
                self.assertIn("中文触发：", description)
                for anchor in case.trigger_anchors:
                    self.assertIn(anchor, description)
                self.assertTrue(
                    any(anchor.casefold() in case.prompt.casefold() for anchor in case.trigger_anchors),
                    f"trigger prompt has no declared anchor: {case.prompt}",
                )
                self.assertIn(case.confusion_skill, CASE_BY_NAME)
                self.assertNotEqual(case.confusion_skill, case.name)

    def test_user_facing_triggers_are_owned_by_the_entry_skill(self) -> None:
        descriptions = self.runtime_descriptions()
        for trigger in ENTRY_ONLY_TRIGGERS:
            with self.subTest(trigger=trigger):
                owners = {
                    name
                    for name, description in descriptions.items()
                    if trigger.casefold() in description.casefold()
                }
                self.assertEqual(owners, {"skyrim-mod-chs-translation"})

    def test_internal_skill_descriptions_declare_entry_preconditions(self) -> None:
        descriptions = self.runtime_descriptions()
        for skill_name, precondition in INTERNAL_SKILL_PRECONDITIONS.items():
            with self.subTest(skill=skill_name):
                self.assertIn(precondition, descriptions[skill_name])

    def test_effect_skill_count_excludes_ignored_local_tool_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            (root / ".gitignore").write_text("*openspec-*\n", encoding="utf-8")
            skill_root = root / ".codex" / "skills"
            tracked_skill = skill_root / "skyrim-mod-chs-usage" / "SKILL.md"
            local_skill = skill_root / "openspec-explore" / "SKILL.md"
            tracked_skill.parent.mkdir(parents=True)
            local_skill.parent.mkdir(parents=True)
            content = "---\nname: fixture\ndescription: fixture skill\n---\n"
            tracked_skill.write_text(content, encoding="utf-8")
            local_skill.write_text(content, encoding="utf-8")

            self.assertEqual(
                count_skill_frontmatter(
                    root,
                    Path(".codex") / "skills",
                    skip_local_tool_skills=True,
                ),
                1,
            )

    def test_non_gui_agents_only_lose_gui_skills(self) -> None:
        expected_gui = {"lextranslator-gui-automation", "xtranslator-gui-automation"}
        for agent in ("codex", "opencode", "claude-code"):
            with self.subTest(agent=agent):
                rows = skill_rows(agent)
                unusable = {str(row["skill_dir"]) for row in rows if not row["usable"]}
                self.assertEqual(unusable, set() if agent == "codex" else expected_gui)

    def test_state_refresh_does_not_implicitly_run_strict_qa(self) -> None:
        policy_skill = read_skill(CASE_BY_NAME["workflow-policy-and-state"])
        usage_skill = read_skill(CASE_BY_NAME["skyrim-mod-chs-usage"])
        self.assertNotIn("--run-strict-gate", policy_skill)
        self.assertIn("strict gate runs only", policy_skill.casefold())
        self.assertNotIn("test_workflow_health.py --run-strict-gate", usage_skill)
        self.assertIn("普通状态刷新不得附加 `--run-strict-gate`", usage_skill)

    def test_plugin_skill_only_requires_master_context_for_light_targets(self) -> None:
        plugin_skill = read_skill(CASE_BY_NAME["esp-esm-esl-translation"])
        self.assertIn("只有当前目标插件为 `.esl` 或带 light trait 时", plugin_skill)
        self.assertIn("普通非 Light 目标插件按 full master 语义处理", plugin_skill)
        self.assertIn("不得要求用户复制 `Skyrim.esm`", plugin_skill)
        self.assertNotIn("缺少的普通 `.esp/.esm` master 副本应放入", plugin_skill)

    def test_fallout4_task7_skill_contracts_are_explicit(self) -> None:
        router = read_skill(CASE_BY_NAME["translation-task-router"])
        route_rules = router.split("## 路由规则", 1)[1].split(
            "## Fallout 4 Data 资源边界", 1
        )[0]
        for contract in (
            "Agent Structured MCM Extractor",
            "Codex-only LexTranslator fallback",
            "Agent Text Pipeline",
            "Structured TOML manual review",
            "INI/TOML 整行注释只读提取",
        ):
            self.assertIn(contract, route_rules)

        fallout4_boundary = router.split("## Fallout 4 Data 资源边界", 1)[1].split(
            "## 推荐工具", 1
        )[0]
        self.assertIn("最终按 protected 处理", fallout4_boundary)
        self.assertIn("否则命中 F4SE 时按 f4se 处理", fallout4_boundary)
        self.assertIn("MCM/Scripts", fallout4_boundary)

        mcm = read_skill(CASE_BY_NAME["mcm-translation"])
        completion = mcm.split("## 完成标准", 1)[1].split("## 失败处理", 1)[0]
        for contract in (
            "`MCM/**/*.json`、`MCM/**/*.ini` 已由 Agent Structured MCM Extractor 处理",
            "`MCM/**/*.txt` 已由 Agent Text Pipeline 处理",
            "`MCM/**/*.toml` 已明确记录为 manual review",
            "自动处理与 manual review 结果已分别记录",
        ):
            self.assertIn(contract, completion)

        text_skill = read_skill(CASE_BY_NAME["text-resource-translation"])
        container_contract = text_skill.split("## Container 边界", 1)[1].split("## 模型翻译要求", 1)[0]
        self.assertIn("protected container 必须先于扩展名和 category/subtype 提取规则判定", container_contract)
        self.assertIn("所有扩展名", container_contract)
        self.assertIn("F4SE 配置的 key/value 只生成结构化人工确认记录", container_contract)
        self.assertIn("INI/TOML 整行注释可进入只读候选包", container_contract)
        self.assertIn("MCM TOML 当前只允许 manual review", container_contract)

        mcm_sources = mcm.split("## 来源识别规则", 1)[1].split("## 可翻译内容", 1)[0]
        self.assertIn("protected 或 F4SE", mcm_sources)
        self.assertIn("不能覆盖外层 container", mcm_sources)


class SkillRuntimeEffectTests(unittest.TestCase):
    def test_every_skill_probe_is_a_real_documented_entrypoint(self) -> None:
        for case in CASES:
            with self.subTest(skill=case.name):
                skill_text = read_skill(case)
                if not case.probe_script:
                    self.assertEqual(case.name, "skyrim-mod-chs-translation")
                    self.assertIn("## Delegation Contract", skill_text)
                    continue
                self.assertIn(case.probe_script, skill_text)
                self.assertTrue((ROOT / case.probe_script).is_file(), case.probe_script)

    def test_every_skill_probe_parses_its_cli_contract(self) -> None:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        for case in CASES:
            if not case.probe_script:
                continue
            with self.subTest(skill=case.name, probe=case.probe_script):
                result = subprocess.run(
                    [sys.executable, str(ROOT / case.probe_script), "--help"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=30,
                    check=False,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("usage:", output.casefold())

    def test_all_documented_script_entrypoints_exist(self) -> None:
        for case in CASES:
            text = read_skill(case)
            references = sorted(set(SCRIPT_REFERENCE_RE.findall(text)))
            with self.subTest(skill=case.name):
                if case.name == "skyrim-mod-chs-translation":
                    self.assertEqual(references, [], "natural-language entry must delegate instead of exposing script ordering")
                    continue
                self.assertTrue(references, "Skill has no deterministic script reference")
                missing = [reference for reference in references if not (ROOT / reference).is_file()]
                self.assertEqual(missing, [])

    def test_gui_skills_record_safe_blocked_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "config").mkdir()
            (workspace / "work").mkdir()
            (workspace / "qa").mkdir()
            (workspace / "config" / "tools.local.json").write_text(
                '{"AllowLaunchGuiTools": false}\n',
                encoding="utf-8",
            )
            (workspace / "work" / "gui-smoke.txt").write_text("fixture\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONUTF8": "1",
                    "SKYRIM_CHS_WORKSPACE_ROOT": str(workspace),
                    "SKYRIM_CHS_PLUGIN_ROOT": str(ROOT),
                }
            )

            xtranslator = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "invoke_xtranslator.py"), "--input-path", "work/gui-smoke.txt"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=30,
                check=False,
            )
            self.assertEqual(xtranslator.returncode, 0, xtranslator.stdout + xtranslator.stderr)

            lextranslator = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "invoke_lextranslator_gui.py"),
                    "--input-path",
                    "work/gui-smoke.txt",
                    "--mode",
                    "inspect",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=30,
                check=False,
            )
            self.assertEqual(lextranslator.returncode, 2, lextranslator.stdout + lextranslator.stderr)
            tool_log = (workspace / "qa" / "tool_invocation_log.md").read_text(encoding="utf-8")
            self.assertIn("Tool: xTranslator", tool_log)
            self.assertIn("Tool: LexTranslator", tool_log)
            report = (workspace / "qa" / "lextranslator_gui_report.md").read_text(encoding="utf-8")
            self.assertIn("Status: blocked", report)
            self.assertIn("No GUI process was launched", report)


if __name__ == "__main__":
    unittest.main()
