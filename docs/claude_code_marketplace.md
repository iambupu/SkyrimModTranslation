# Claude Code Marketplace

这个仓库提供 Claude Code marketplace 配置，但只暴露非 GUI Skill。Codex 仍是默认入口，也是唯一能处理 GUI、Computer Use、pywinauto/UI Automation、LexTranslator/xTranslator 桌面操作和 `gui:desktop` 锁的入口。

Claude Code marketplace 元数据位于：

| 文件 | 用途 |
|---|---|
| `.claude-plugin/marketplace.json` | Claude Code marketplace 入口，声明 `skyrim-mod-chs` marketplace 和非 GUI Skill 列表 |
| `.claude-plugin/plugin.json` | Claude Code plugin 元数据，不声明组件 |

安装命令和接手步骤只在 [Claude Code Adapter](./claude_code_adapter.md#安装动作) 维护。本页只说明 marketplace 暴露范围和元数据约束。

## 暴露范围

Marketplace 使用 `strict=true` 和明确的 `skills` 列表，让 Claude Code 只注册根目录 `skills/` 中列出的非 GUI Skill，并避免自动发现与 marketplace 组件清单冲突。当前排除：

- `skills/lextranslator-gui-automation`
- `skills/xtranslator-gui-automation`

Claude Code 可以读取项目规则、运行非 GUI Python 脚本、写 QA 报告和接手证据。它是顶层入口，不直接领取子任务；`qa/workflow_tasks.json` 的任务由主控分派给子 agent。遇到需要 GUI 的步骤时，必须记录 blocked，并设置 `handoff_target=codex`。

## 维护规则

- 不要在 `.claude-plugin/plugin.json` 声明 `skills`、`commands`、`agents`、`hooks` 或 `mcpServers`；组件由 marketplace 的非 GUI 列表控制。
- 不要在 `.claude-plugin/marketplace.json` 的 plugin entry 中声明 `commands`、`agents`、`hooks` 或 `mcpServers`；Claude marketplace 只暴露经过筛选的非 GUI `skills`。
- 不要复制第二套 Skills。根目录 `skills/` 仍是唯一运行 Skill 来源。
- 不要在 Claude marketplace 中加入 GUI Skill。
- marketplace plugin entry 必须声明 `version`，并与 `.claude-plugin/plugin.json`、`.codex-plugin/plugin.json` 和 `pyproject.toml` 保持一致；发布时统一更新。
- 不要把 Claude marketplace 校验挂到 Codex 默认翻译流程。

验证：

```powershell
python scripts\validate_claude_plugin_marketplace.py
python scripts\ci_validate_repo.py --strict
```

官方参考：

- [Claude Code plugins](https://docs.claude.com/en/docs/claude-code/plugins)
- [Claude Code plugin marketplaces](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces)
- [Claude Code plugins reference](https://docs.claude.com/en/docs/claude-code/plugins-reference)
