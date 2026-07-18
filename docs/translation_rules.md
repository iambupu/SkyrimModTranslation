# Translation Rules

## 语言

- 使用简体中文。
- 保持自然游戏本地化风格。
- 不要机翻腔。
- 根据当前 Game Profile 和作品语境保持风格，不要把奇幻、废土、科幻或其他设定文本翻成现代网络用语。
- 不要擅自改写剧情含义。

## 类型规则

- UI/MCM 文本：短而准确。
- 书籍文本：可以更文学，但不能改变信息。
- 对话文本：符合角色身份。
- 任务目标：清晰直接。
- 物品名：短，像游戏内名称。
- 法术、能力、Perk 和效果名：贴合当前游戏设定；Skyrim 保留奇幻感，Fallout 4 保留废土、科技和复古未来语境。
- 技能/效果说明：准确优先。

## 禁止翻译

- FormID
- EditorID
- 脚本名
- 变量名
- 路径
- 文件名
- 插件名
- JSON key
- XML/HTML tag
- 函数名
- 属性名
- 状态名
- 事件名
- 脚本内部 key
- page id
- state id
- StorageUtil key
- JsonUtil key
- 任何可能参与 if 判断、switch 判断、数组索引、字典 key 的字符串

## 必须保留

- 占位符
- 换行符
- 颜色标签
- 字体标签
- HTML/XML 标签
- MCM 控制符
- Papyrus 变量标记

## Fallout 4 Data 资源

- MCM 是 container，不是单一 JSON 格式。`MCM/` 内资源要按 JSON、INI、TOML、TXT、Interface 文本、插件导出或 PEX 导出的实际格式处理。
- JSON、INI、TOML 只翻译结构化确认的玩家可见 value。必须保留 key、路径、协议值和内部标识。
- F4SE DLL 不修改。`F4SE/` 下的 INI/TOML 整行注释可只读提取为翻译候选；key/value 仍须确认用途，JSON value 不做通用自动提取。
- SWF/GFX 只做 inventory 和人工检查。优先翻译 `Interface/translations/*.txt`，不得反编译后回写 SWF/GFX。
- Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq 下的资源默认原样复制，不进入翻译管线。
- Skyrim/Fallout 4 `.esl` 和带 light trait 的插件可按 `experimental_write` 受控写回，但必须具备完整的工作区 master-style 与 canonical FormKey 证据。STRINGS、DLSTRINGS、ILSTRINGS 固定走专用 adapter；Fallout 4 为实验级。Localized 插件固定走 `localized_delivery` 复合能力，不能由普通插件或字符串表证据单独放行。

## Skyrim 插件边界

- 普通非 localized ESP/ESM 使用受控字段合同导出和写回。
- `.esl` 或带 light trait 的 `.esp/.esm` 当前为实验性写回；工作区内 master style、owner ModKey、local ID 或 source evidence 不完整时必须阻断。

## 不确定术语

- 写入 `qa/unresolved_terms.md`。
- 不要擅自硬翻。
- 需要用户确认后再进入工作区 `glossary/mod_terms.md`。

## RAG 词典

- 词典是强烈推荐的质量增强项，不是翻译、QA 或交付的必需输入。缺失或无法解析时记录原因并继续翻译。
- 只使用当前 Game Profile 的 `glossary_sources`。不同游戏使用独立目录；不得扫描整个 `glossary/`。
- 支持 Markdown、LexTranslator 风格 TXT/CSV/DICT、xTranslator SST 和 ESP-ESM Translator EET；SST/EET 只读解码。
- 翻译前通过插件源脚本 `scripts/build_external_glossary_matches.py` 为当前 Mod 生成命中词表；脚本输出写回当前工作区。
- 命中词表只作为术语提示，不是自动替换表；上下文冲突时记录到 `qa/unresolved_terms.md`。
- 游戏、源清单、索引版本或词典内容变化时重建索引；详情见 `docs/lextranslator_dictionary_rag.md`。

## Papyrus 可见文本

- 可以翻译玩家可见的通知、菜单、说明、MessageBox、MCM 文本。
- 可以分析 `mod/` 目录下的 `Interface/translations/*.txt`。
- 可以分析 `mod/` 目录下导出的 MCM 文本。
- 可以处理 LexTranslator 或 xTranslator 从 `.pex` 中导出的可翻译字符串。
- 翻译结果只能输出到 `translated/` 或 `out/`。
- 不直接修改 `.pex` 文件。
- 不直接修改 `.psc` 源码并重新编译。
- 不覆盖 `mod/` 下原始脚本文件。
- 如果必须查看 `.psc`，只提取字符串字面量供人工确认，不自动回写源码。
- 所有脚本翻译结果必须经过人工抽查和游戏内测试。

优先级：

1. 如果存在 `Interface/translations/*.txt`，优先翻译这些文件，不碰 `.pex`。
2. 如果没有独立翻译文件，优先用 `PexStringToolPath` / Mutagen PEX 适配器提取 `.pex` 中的可见字符串。
3. 如果必须处理 `.psc`，只允许提取字符串字面量供人工确认，不自动回写源码，不自动编译。
