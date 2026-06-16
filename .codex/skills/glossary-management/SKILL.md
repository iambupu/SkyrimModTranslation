---
name: glossary-management
description: Use when managing Skyrim Chinese glossary, current Mod terms, unresolved proper nouns, and terminology consistency. Do not use for file routing, GUI automation, binary editing, or final_mod assembly.
---

# Glossary Management

## 目标

维护术语一致性，管理 `glossary/` 和 `qa/unresolved_terms.md`。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前项目内。
- Mod 原始输入只允许来自当前项目 `mod/` 沙盒。
- 不访问真实 Skyrim 游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不直接修改插件二进制。

## 触发条件

- 开始新 Mod。
- 批量翻译前后。
- 出现专有名词、不确定译名或术语冲突。

## 输入

- `glossary/skyrim_cn_glossary.md`
- `glossary/mod_terms.md`
- 翻译任务文本。
- QA 未决术语。

## 输出

- 更新后的 glossary。
- `qa/unresolved_terms.md`。

## 推荐工具

- Codex Text Pipeline。
- Codex 模型术语判断。

## 具体流程

1. 优先查 `mod_terms.md`。
2. 再查 `skyrim_cn_glossary.md`。
3. 结合当前任务上下文。
4. 使用 Codex 模型判断术语是否应翻译、保留英文、音译或意译。
5. 不确定项写入 `qa/unresolved_terms.md`。
6. 用户确认或上下文充分后再进入 `mod_terms.md`。

## 禁止事项

- 不硬翻不确定专有名词。
- 不把 FormID、EditorID、脚本名、路径、文件名当术语翻译。

## QA 检查

- 术语一致性。
- 未决术语有上下文。
- 暂定和确认状态分开。

## 完成标准

- 已优先查阅 `glossary/mod_terms.md` 和 `glossary/skyrim_cn_glossary.md`。
- 新增术语有来源上下文和状态，未确认项未被硬翻。
- `qa/unresolved_terms.md` 已记录仍需人工确认的专有名词。
- 翻译批次可引用一致术语，不确定项保留待审状态。

## 失败处理

上下文不足时保留英文或暂定译名，并记录未决。
