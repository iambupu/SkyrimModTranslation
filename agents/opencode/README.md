# opencode Adapter

opencode 是 Skyrim CHS workflow 的非 GUI 顶层主控入口。Skyrim SE/AE 是默认完整流程，Fallout 4 Experimental 只按工作区 Game Profile 暴露已声明能力。游戏身份以 marker/profile 为准，不按 Mod 名猜测。

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

opencode 顶层主控不领取子任务，也不直接编辑 `qa/workflow_tasks.json`。只有主控派生的子智能体可按 `workflow-subagent-orchestration` 使用 `claim_workflow_task.py`。

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

The supported bootstrap entrypoint is:

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

It writes workspace-local `opencode.json`, `.opencode/` files, and a local `.opencode/plugins/skyrim-chs.js` plugin. The local plugin injects workspace environment variables and resume context only; it does not add GUI capability or copy plugin-source scripts/runtime Skills into the workspace.
