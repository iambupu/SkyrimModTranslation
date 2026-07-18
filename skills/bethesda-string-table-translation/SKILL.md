---
name: bethesda-string-table-translation
description: "用于按当前 Game Profile 处理 Bethesda STRINGS、DLSTRINGS、ILSTRINGS 的受控 Inventory、Export、Apply 和 Verify。中文触发：STRINGS、DLSTRINGS、ILSTRINGS、字符串表、string ID、localized 插件外部文本。Never generic-decode, edit binary bytes directly, or use GUI availability to raise capability levels."
---

# Bethesda String Table Translation

## 目标

处理 `.strings`、`.dlstrings` 和 `.ilstrings` 的受控导出、翻译写回和独立验证。本 Skill 不自行完成 localized 插件联合交付；发现对应 localized 插件时，必须把已验证组件交回 `esp-esm-esl-translation` 使用 `localized_delivery` composite adapter。

## 硬约束

- 运行环境为 Windows，命令入口统一使用 Python 和 PowerShell 参数形式，不引入 Bash/WSL 包装层。
- 游戏身份、源编码、目标编码、源语言 token 和目标语言 token 只取工作区 marker 与当前 Game Profile。
- 字符串表是受保护二进制。Agent 不得直接修改，也不得把它当成 TXT 或其他普通文本处理。
- 只使用 Adapter Registry 为 `string_tables` 返回的操作入口。当前内置入口是 `scripts/invoke_bethesda_string_table_tool.py`。
- `inventory_only` 只允许 Inventory；`read_only` 允许 Export；Apply 需要 `experimental_write` 或 `stable`；严格完成仍按 Profile 能力判定。
- xTranslator/LexTranslator 或其他 GUI 工具可用于人工对照，但不能提升 capability，也不能替代 AdapterResult、Verify 和最终交付证据。

## 标准流程

1. Inventory：验证 header、目录、ID、offset、长度、终止符、文件边界、文件名语言 token、大小和 SHA256。
2. Export：生成 `source/string_tables/<ModName>/` 下的 schema v2 JSONL。
3. 翻译：只填写 `Result`；不得修改 game、插件 basename、表类型、语言、string ID、source、源路径或源 SHA256。
4. Apply：输入译表必须位于 `translated/<kind>/<ModName>/` 或 `work/normalized/<ModName>/`；输出只能进入受控 `tool_outputs/Strings/`。
5. Verify：必须传入成功的 Apply AdapterResult，独立反读并验证 ID 集合、精确目标和未授权值不变。

## 命令形态

```powershell
python .\scripts\invoke_bethesda_string_table_tool.py --mode Inventory --input-table-path "work\extracted_mods\<ModName>\Strings\<Table>" --output-json-path "qa\<ModName>.string_table_inventory.json" --report-path "qa\<ModName>.string_table_inventory.md"

python .\scripts\invoke_bethesda_string_table_tool.py --mode Export --input-table-path "work\extracted_mods\<ModName>\Strings\<Table>" --output-jsonl-path "source\string_tables\<ModName>\<Table>.jsonl" --report-path "qa\<ModName>.string_table_export.md"
```

Apply 和 Verify 只能在当前 Profile 的写能力已启用时运行；实验能力还必须显式传入 `--allow-experimental-writeback`，Verify 必须提供 `--apply-adapter-result-path`。

## 完成标准

- 输出文件名使用当前 Profile 的目标语言 token。
- Apply receipt 绑定源表、译表、输出表和报告 hash。
- Verify 独立反读成功，ID 集合和数量不变，只有授权 ID 的逻辑值变化。
- 输出只存在于受控 `tool_outputs`；普通 overlay 中不存在修改后的字符串表。
- 单独字符串表结果不得宣称为完整 localized Mod 交付；必须由 `localized_delivery` 复合验证插件锚点、引用覆盖和全部组件。
