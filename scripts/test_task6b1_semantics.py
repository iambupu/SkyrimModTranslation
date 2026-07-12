from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import export_agent_context  # noqa: E402


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def description(relative_path: str) -> str:
    text = read(relative_path)
    frontmatter = text.split("---", 2)[1]
    for line in frontmatter.splitlines():
        if line.startswith("description:"):
            return line.removeprefix("description:").strip().strip('"')
    raise AssertionError(f"missing description: {relative_path}")


class Task6B1SkillSemanticsTests(unittest.TestCase):
    def test_entry_and_router_descriptions_are_profile_aware(self) -> None:
        entry = description("skills/skyrim-mod-chs-translation/SKILL.md")
        router = description("skills/translation-task-router/SKILL.md")

        self.assertIn("Skyrim SE/AE", entry)
        self.assertIn("Fallout 4 Experimental", entry)
        self.assertIn("Game Profile", entry)
        self.assertIn("Bethesda Mod", router)
        self.assertIn("Game Profile", router)

    def test_ba2_routes_to_the_ba2_skill_and_bsa_does_not_materialize_it(self) -> None:
        router = read("skills/translation-task-router/SKILL.md")
        input_skill = read("skills/mod-input-preparation/SKILL.md")
        bsa_skill = read("skills/bsa-archive-audit/SKILL.md")
        agents = read("AGENTS.md")

        self.assertRegex(router, r"\| `\.ba2` \|[^\n]+`ba2-archive-audit`")
        self.assertIn("BA2 materialization", bsa_skill)
        self.assertIn("`ba2-archive-audit`", bsa_skill)
        self.assertIn("`.ba2` 交给 `ba2-archive-audit`", input_skill)
        self.assertIn("| `.ba2`", agents)
        self.assertIn("`ba2-archive-audit`", agents)
        self.assertNotIn("未来 BA2 adapter", router)
        self.assertNotIn("`.bsa/.ba2` 必须路由给 `bsa-archive-audit`", input_skill)

    def test_game_specific_fail_closed_contracts_are_explicit(self) -> None:
        plugin = read("skills/esp-esm-esl-translation/SKILL.md")
        pex = read("skills/pex-visible-strings-translation/SKILL.md")
        ba2 = read("skills/ba2-archive-audit/SKILL.md")
        final_skill = read("skills/final-mod-assembly/SKILL.md")
        agents = read("AGENTS.md")

        self.assertIn("localized", plugin)
        self.assertIn("STRINGS", plugin)
        self.assertIn("blocked", plugin)
        self.assertIn("Fallout4Mod", plugin)
        self.assertIn("Export", pex)
        self.assertIn("experimental opt-in", pex)
        self.assertIn("strict", pex)
        self.assertIn("loose override", ba2)
        self.assertIn("不重打包", ba2)
        self.assertIn("当前 Game Profile 的 Data 根", final_skill)
        for suffix in ("`.swf`", "`.dll`", "`.exe`", "localized plugin", "STRINGS"):
            self.assertIn(suffix, agents)

    def test_runtime_skill_descriptions_are_independently_routable(self) -> None:
        expected_terms = {
            "skills/mod-input-preparation/SKILL.md": ("Game Profile", "BA2"),
            "skills/bsa-archive-audit/SKILL.md": ("BSA", "BA2 materialization"),
            "skills/esp-esm-esl-translation/SKILL.md": ("ESP/ESM/ESL", "Fallout 4"),
            "skills/pex-visible-strings-translation/SKILL.md": ("PEX", "experimental"),
            "skills/mcm-translation/SKILL.md": ("MCM", "Game Profile"),
            "skills/text-resource-translation/SKILL.md": ("文本", "Game Profile"),
            "skills/glossary-management/SKILL.md": ("术语", "Game Profile"),
            "skills/qa-validation/SKILL.md": ("QA", "game/profile"),
            "skills/final-mod-assembly/SKILL.md": ("final_mod", "Game Profile"),
            "skills/skyrim-mod-translation-orchestrator/SKILL.md": ("运行期", "Game Profile"),
            "skills/workspace-tool-setup/SKILL.md": ("工具", "Fallout 4 Experimental"),
        }
        for path, terms in expected_terms.items():
            with self.subTest(path=path):
                value = description(path)
                for term in terms:
                    self.assertIn(term, value)


class Task6B1AgentEntrySemanticsTests(unittest.TestCase):
    def test_game_context_summary_is_an_allowlisted_short_packet(self) -> None:
        payload = {
            field: f"value-{field}"
            for field in export_agent_context.GAME_CONTEXT_FIELDS
        }
        payload["large_unrelated_payload"] = "x" * 20000
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff = Path(temp_dir) / "agent_handoff.json"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            summary = export_agent_context.read_game_context_summary(handoff)

        self.assertEqual(list(summary), list(export_agent_context.GAME_CONTEXT_FIELDS))
        self.assertNotIn("large_unrelated_payload", summary)

    def test_non_gui_agent_prompts_keep_controller_and_gui_boundaries(self) -> None:
        for adapter in ("opencode", "claude-code"):
            with self.subTest(adapter=adapter):
                prompt = read(f"agents/{adapter}/prompt.md")
                readme = read(f"agents/{adapter}/README.md")
                combined = prompt + readme
                self.assertIn("Skyrim SE/AE", combined)
                self.assertIn("Fallout 4 Experimental", combined)
                self.assertIn("Game Profile", combined)
                self.assertIn("非 GUI 顶层主控", combined)
                self.assertIn("不领取", combined)
                self.assertIn("Codex", combined)

    def test_plugin_descriptions_expose_default_and_experimental_scope(self) -> None:
        manifests = (
            ".codex-plugin/plugin.json",
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
        )
        for path in manifests:
            with self.subTest(path=path):
                payload = json.loads(read(path))
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertIn("Skyrim SE/AE", serialized)
                self.assertIn("Fallout 4 Experimental", serialized)

        codex = json.loads(read(".codex-plugin/plugin.json"))
        serialized = json.dumps(codex, ensure_ascii=False).lower()
        self.assertIn("fallout 4", serialized)

    def test_agent_context_export_is_explicit_and_stays_out_of_hot_path(self) -> None:
        export_script = read("scripts/export_agent_context.py")
        init_script = read("scripts/init_opencode.py")
        agents = read("AGENTS.md")

        for field in ("game_id", "game_profile_version", "game_display_name", "support_level"):
            self.assertIn(field, export_script)
        self.assertIn("workspace marker", init_script)
        self.assertIn("Mod 名", init_script)
        self.assertIn("write_agent_handoff.py", agents)
        self.assertIn("显式", agents)
        self.assertIn("默认翻译热路径", agents)


if __name__ == "__main__":
    unittest.main()
