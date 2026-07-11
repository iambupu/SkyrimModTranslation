# Claude Code Adapter

Claude Code is a first-class non-GUI adapter for the Skyrim CHS workflow core. It can be installed through the Claude Code marketplace metadata under `.claude-plugin/`, and it can act as a top-level CLI agent/controller through project Python entrypoints, shared Skills, workflow state, and QA reports.

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

Subtask claiming is not a top-level Claude Code adapter action. It belongs to controller-spawned subagents that use `claim_workflow_task.py` under `workflow-subagent-orchestration`.

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

Claude Code marketplace install:

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

The marketplace exposes non-GUI Skills only. It does not grant Codex GUI, Computer Use, or Codex plugin-call capability.
