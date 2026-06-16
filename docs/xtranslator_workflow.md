# xTranslator Workflow

- xTranslator 适合做精修、查漏、对照、回写。
- 推荐用于检查 ESP/ESM、Strings、MCM/Translate、PapyrusPex 文本。
- 对复杂插件，先用 xTranslator 查看文本结构和未翻译项。
- 可以导出文本供 Codex 分析。
- Codex 可以通过 Computer Use 自动操作 xTranslator GUI，但所有输入和输出路径必须位于当前项目内。
- Codex 不绕过 xTranslator 直接保存插件；插件输出必须由 xTranslator 生成到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。
- 保存插件输出前，必须备份原插件或使用独立 Patch Mod。
- 推荐每次只处理一个插件，避免批量误伤。
- 不要让 Codex 直接操作真实 MO2/Vortex 目录。
- 如果需要处理 PapyrusPex 文本，只让 xTranslator 提取玩家可见字符串；Codex 只处理导出的文本，不直接修改 `.pex`。
- 不翻译函数名、变量名、属性名、状态名、事件名、StorageUtil key、JsonUtil key 或任何可能参与脚本判断的字符串。
- 所有从 `.pex` 导出的脚本文本必须经过人工抽查和游戏内测试。

## 配置来源

- xTranslator 可执行文件路径只从 `config/tools.local.json` 读取。
- `config/tools.local.json` 是本地配置，不提交到远程。
- GUI fallback 前先运行工具配置校验；路径缺失或不可访问时，工具阶段标记为 blocked。
- 如果 xTranslator 配置或偏好里出现真实游戏、Steam、MO2/Vortex、AppData 或 Documents/My Games 路径，GUI 保存必须继续限制在项目内输出副本。

## GUI fallback 要求

- xTranslator 只在路由明确进入 GUI fallback、精修、查漏、对照、复杂导入或 PapyrusPex 后备时使用。
- 保存或导出目标只能位于 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- 工具输出后必须运行对应验证脚本；插件输出运行 `python scripts/verify_plugin_output.py`，PEX 输出运行 `python scripts/verify_pex_output.py`。
- 只打开窗口、只加载文件或只完成检查不算翻译完成。
- GUI 保存路径不可确认时，立即停止并标记 blocked。
