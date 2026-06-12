# mod/ Sandbox Rules

- `mod/` 是当前项目唯一允许处理的 Mod 输入目录。
- `mod/` 必须是从真实 Mod 管理器复制出来的副本。
- `mod/` 不是游戏实际加载目录。
- Codex 可以读取 `mod/`。
- Codex 可以扫描 `mod/` 文件结构。
- Codex 可以复制 `mod/` 下的文本导出文件到 `source/`。
- Codex 不能直接修改 `mod/` 下的插件二进制文件。
- Codex 不能把输出写回真实游戏目录。
- 所有输出必须写入项目内目录。
- 最终导入 LexTranslator/xTranslator 由用户手动执行。
- 最终复制到 MO2/Vortex 由用户手动执行。

