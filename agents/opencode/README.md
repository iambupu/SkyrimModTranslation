# opencode Adapter

opencode is a first-class non-GUI adapter for the Skyrim CHS workflow core. It can act as a top-level CLI agent/controller through project Python entrypoints, shared Skills, workflow state, and QA reports.

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

Subtask claiming is not an opencode adapter feature. It belongs to controller-spawned subagents that use `claim_workflow_task.py` under `workflow-agent-orchestration`.

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

The supported bootstrap entrypoint is:

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

It writes workspace-local `opencode.json` and `.opencode/` files, refreshes the bounded opencode context packet, and can start either the opencode TUI or a one-shot `opencode run`. It does not copy plugin-source scripts or runtime Skills into the workspace.
