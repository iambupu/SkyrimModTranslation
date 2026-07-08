# Claude Code Marketplace

本项目支持 Claude Code marketplace，但只暴露非 GUI workflow 能力。Codex 仍是默认完整插件入口，并且独占 GUI、Computer Use、pywinauto/UI Automation、LexTranslator/xTranslator 桌面操作和 `gui:desktop` 锁。

Claude Code marketplace 元数据位于：

| 文件 | 用途 |
|---|---|
| `.claude-plugin/marketplace.json` | Claude Code marketplace 入口，声明 `skyrim-mod-chs` marketplace 和非 GUI Skill 列表 |
| `.claude-plugin/plugin.json` | Claude Code plugin 元数据，不声明组件 |

## 安装

从 GitHub `master` 分支添加 marketplace：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

从本地仓库调试：

```text
/plugin marketplace add D:\bupuy\Documents\SkyrimModTranslation
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

这些命令属于 Claude Code 的 `/plugin`，不是 Codex 的 `codex plugin marketplace add`。

## 暴露范围

Marketplace 使用 `strict=false` 和显式 `skills` 列表，让 Claude Code 只加载根目录 `skills/` 中的非 GUI Skill。当前排除：

- `skills/lextranslator-gui-automation`
- `skills/xtranslator-gui-automation`

Claude Code 可以读取项目规则、运行非 GUI Python workflow、写 QA 报告和 handoff 证据。它是顶层非 GUI adapter，不直接领取子任务；`qa/workflow_tasks.json` 的子任务领取属于主控分派的子 agent。遇到 GUI-only 步骤必须记录 blocked，并设置 `handoff_target=codex`。

## 维护规则

- 不要在 `.claude-plugin/plugin.json` 声明 `skills`、`commands`、`agents`、`hooks` 或 `mcpServers`；组件由 marketplace 的非 GUI 列表控制。
- 不要在 `.claude-plugin/marketplace.json` 的 plugin entry 中声明 `commands`、`agents`、`hooks` 或 `mcpServers`；Claude marketplace 只暴露 curated 非 GUI `skills`。
- 不要复制第二套 Skills。根目录 `skills/` 仍是唯一运行 Skill 来源。
- 不要在 Claude marketplace 中加入 GUI Skill。
- 不要给 Claude plugin 固定 `version`，GitHub 安装应按 commit 更新；发布前如果要改成版本 pin，必须同步校验脚本和发布流程。
- 不要把 Claude marketplace 校验挂到 Codex 默认翻译热路径。

验证：

```console
python scripts\validate_claude_plugin_marketplace.py
python scripts\ci_validate_repo.py --strict
```

官方参考：

- [Claude Code plugins](https://docs.claude.com/en/docs/claude-code/plugins)
- [Claude Code plugin marketplaces](https://docs.claude.com/en/docs/claude-code/plugin-marketplaces)
- [Claude Code plugins reference](https://docs.claude.com/en/docs/claude-code/plugins-reference)
