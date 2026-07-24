# opencode Adapter

受支持的运行环境是 Windows；命令通过 PowerShell 和插件源 Python 入口执行。

opencode 是 Skyrim CHS workflow 的非 GUI 顶层主控入口。Skyrim SE/AE 提供稳定完整支持，Fallout 4 提供 Experimental Support；新工作区没有默认游戏，实际能力只由工作区 marker 和 Game Profile 决定，不按 Mod 名猜测。

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

opencode 顶层主控只调用公开
`smt.py --format json run|status|resume|doctor|output`，不直接读取或编辑
`qa/workflow_tasks.json` 来选择底层动作。只有公开控制器内部明确分派的
子智能体可按 `workflow-subagent-orchestration` 使用任务领取协议。

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

仅在用户明确要求安装或刷新 opencode 本地适配器时，使用：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

It writes workspace-local `opencode.json`, `.opencode/` files, and a local `.opencode/plugins/skyrim-chs.js` plugin. The local plugin injects workspace environment variables and resume context only; it does not add GUI capability or copy plugin-source scripts/runtime Skills into the workspace.
