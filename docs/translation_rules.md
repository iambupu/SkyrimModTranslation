# Translation Rules

## 语言

- 使用简体中文。
- 保持自然游戏本地化风格。
- 不要机翻腔。
- 不要把奇幻文本翻成现代网络用语。
- 不要擅自改写剧情含义。

## 类型规则

- UI/MCM 文本：短而准确。
- 书籍文本：可以更文学，但不能改变信息。
- 对话文本：符合角色身份。
- 任务目标：清晰直接。
- 物品名：短，像游戏内名称。
- 法术名：保留奇幻感。
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

## 不确定术语

- 写入 `qa/unresolved_terms.md`。
- 不要擅自硬翻。
- 需要用户确认后再进入工作区 `glossary/mod_terms.md`。

## 动态词典

- LexTranslator 风格词表放在当前工作区 `glossary/lextranslator_dynamic_dictionaries/`，可以按来源新增文件或子目录。
- 翻译前通过插件源脚本 `scripts/build_external_glossary_matches.py` 为当前 Mod 生成命中词表；脚本输出写回当前工作区。
- 命中词表只作为术语提示，不是自动替换表；上下文冲突时记录到 `qa/unresolved_terms.md`。
- 索引刷新规则见 `docs/lextranslator_dictionary_rag.md`。

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
