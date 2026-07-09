# opencode Adapter

opencode is the non-GUI CLI entry for the Skyrim CHS workflow. It can act as a top-level controller through project Python entrypoints, shared Skills, workflow state, and QA reports.

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

Subtask claiming is not an opencode feature. It belongs to controller-spawned subagents that use `claim_workflow_task.py` under `workflow-agent-orchestration`.

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

The supported bootstrap entrypoint is:

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

It writes workspace-local `opencode.json`, `.opencode/` files, and a local `.opencode/plugins/skyrim-chs.js` plugin. The local plugin injects workspace environment variables and resume context only; it does not add GUI capability or copy plugin-source scripts/runtime Skills into the workspace.
