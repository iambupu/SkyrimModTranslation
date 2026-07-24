# Codex Adapter Prompt

Run only in the supported Windows environment. Execute project commands through PowerShell and plugin-source Python entrypoints; do not introduce Bash, WSL, Linux commands, or shell wrappers.

Use the shared Skyrim CHS workflow core. At the top level, call only
`smt.py --format json run|status|resume|doctor|output` and choose actions from
its single JSON result. Do not use workspace handoff, workflow state/tasks,
policy, or internal scripts as a second top-level command source; those remain
inputs for the public controller, runtime Skills, and explicitly delegated
subagents.

Codex may handle GUI-only steps only when the workflow policy and project rules allow it. GUI fallback must still write project-local evidence and must never access real Skyrim, MO2, Vortex, Steam, AppData, or `Documents/My Games` paths.
