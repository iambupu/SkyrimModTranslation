# Codex Adapter Prompt

Run only in the supported Windows environment. Execute project commands through PowerShell and plugin-source Python entrypoints; do not introduce Bash, WSL, Linux commands, or shell wrappers.

Use the shared Skyrim CHS workflow core. Read workspace `qa/codex_handoff.json`, `qa/agent_handoff.json`, `qa/workflow_state.json`, and `qa/workflow_tasks.json`, plus plugin-source `config/workflow_policy.json`, before choosing actions.

Codex may handle GUI-only steps only when the workflow policy and project rules allow it. GUI fallback must still write project-local evidence and must never access real Skyrim, MO2, Vortex, Steam, AppData, or `Documents/My Games` paths.
