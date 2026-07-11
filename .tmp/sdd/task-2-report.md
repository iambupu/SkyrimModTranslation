# Task 2 Report

## Scope

- Task: Fallout 4 Experimental Support / Task 2
- Branch: `codex/fallout4-experimental-support`
- Write scope honored: only brief-listed files were changed
- Preserved unrelated worktree changes: `.gitignore`, `docs/superpowers/`

## TDD Evidence

### RED

Command:

```powershell
python -m pytest -q scripts/test_fallout4_routing_regressions.py
```

Observed expected failures before implementation:

- `Route` payload lacked `game_id`
- `.strings` routed to `manual-review` instead of explicit blocked string-table routing
- Fallout 4 MCM extraction still accepted Skyrim-only keys such as `displayName`
- non-GUI extraction did not emit string-table blockers
- coverage audit treated blockers as generic `unsupported-kind`
- glossary matching did not include current-game/base glossary priority handling
- invalid MCM schema fixture could not be exercised because schema files were missing

### GREEN

Commands:

```powershell
python -m pytest -q scripts/test_fallout4_routing_regressions.py
python -m pytest -q
```

Results:

- `scripts/test_fallout4_routing_regressions.py`: `8 passed, 2 subtests passed`
- full suite: `125 passed`

## Files Changed

- `config/mcm_schemas/skyrim-se.json`
- `config/mcm_schemas/fallout4.json`
- `glossary/fallout4_cn_glossary.md`
- `scripts/route_translation_task.py`
- `scripts/detect_mod_files.py`
- `scripts/extract_mcm_text.py`
- `scripts/extract_non_gui_candidates.py`
- `scripts/audit_non_gui_coverage.py`
- `scripts/build_external_glossary_matches.py`
- `scripts/test_fallout4_routing_regressions.py`

## Self Review

- Routing now resolves current game from workspace marker with Skyrim fallback when marker is absent.
- Plugin/PEX routes now surface `game_id`; Fallout 4 string tables are explicitly blocked with reason `missing string-table adapter`.
- `.swf/.gfx/.dll/.exe` stay in protected/manual routing and do not enter generic text handling.
- MCM extraction now loads per-game schema from `config/mcm_schemas/*.json` and fails closed on missing/invalid schema.
- Fallout 4 glossary matching now defaults to `mod_terms.md > fallout4_cn_glossary.md > lextranslator_dynamic_dictionaries` and excludes Skyrim base glossary by default.
- non-GUI extraction and coverage reports now include `game_id`; Fallout 4 string-table blockers remain visible in coverage instead of disappearing into zero-gap reports.
- Route dataclass compatibility was preserved for existing tests by adding defaults for new fields.

## Concerns

- None at Task 2 scope.
