# Codex Plugin Adapter

Codex Plugin 是默认完整入口，并继续支持 Codex 插件调用。

保留内容：

- `.codex-plugin/plugin.json`
- `.codex/skills/` meta Skills
- 根目录 `skills/` runtime Skills
- `qa/codex_handoff.json`
- Codex progress-card replay contract

Codex 是唯一支持 GUI 的 adapter。LexTranslator/xTranslator GUI、Computer Use、pywinauto 和 UI Automation 只能由 Codex 在项目规则允许时处理。

新增 multi-agent 支持不得降低 Codex 现有性能：Codex 默认流程不做 adapter capability 探测，不导出 adapter context，不强制写通用 handoff。
