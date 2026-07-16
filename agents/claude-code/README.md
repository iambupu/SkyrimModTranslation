# Claude Code Adapter

受支持的运行环境是 Windows；命令通过 PowerShell 和插件源 Python 入口执行。

Claude Code 是 Skyrim CHS workflow 的非 GUI 顶层主控入口。Skyrim SE/AE 提供稳定完整支持，Fallout 4 提供 Experimental Support；新工作区没有默认游戏，实际能力只由工作区 marker 和 Game Profile 决定，不按 Mod 名猜测。

It must not attempt LexTranslator/xTranslator GUI fallback, Computer Use, pywinauto, UI Automation, or fixed desktop coordinates. If a workflow step requires GUI handling, mark it blocked with `handoff_target=codex`.

Claude Code 顶层主控不领取子任务，也不直接编辑 `qa/workflow_tasks.json`。只有主控派生的子智能体可按 `workflow-subagent-orchestration` 使用 `claim_workflow_task.py`。

When running from an initialized workspace, resolve the plugin source path from `.skyrim-chs-workspace.json` and run plugin-source scripts by absolute path. See `docs/agent_compatibility.md`.

Claude Code marketplace install:

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

The marketplace exposes non-GUI Skills only. It does not grant Codex GUI, Computer Use, or Codex plugin-call capability.
