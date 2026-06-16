# Codex Workflow

本文件只作为 Codex 接手入口和文档索引，不重复展开 final_mod、翻译规则或校对门禁细节。

## 默认接手顺序

1. 先读 `AGENTS.md`，确认项目边界和禁止事项。
2. 再读 `qa/workflow_health.md` 或 `qa/workflow_health.json`，确认核心脚本、Skill、严格门禁和最终证据状态。
3. 再读 `qa/translation_readiness.md` 或 `qa/translation_readiness.json`，确认 `mod/` 输入、已知输出、项目级状态和下一条建议命令。
4. 如果 readiness 给出推荐命令，优先执行推荐命令；不要手动拼接分步脚本。
5. 只有状态 blocked、证据缺失或用户明确要求局部处理时，才打开具体规则文档和对应 Skill。

## Codex 可以做

- 只在当前项目内分析 `mod/` 沙盒、`work/` 工作副本、`source/`、`translated/`、`out/` 和 `qa/` 证据。
- 执行项目内 Python 主流程、工具适配器、QA 门禁和 final_mod 组装。
- 维护文档、脚本、配置模板、术语表和 `.codex/skills/`。
- 通过受控 Tool Adapter / Computer Use 操作 LexTranslator 或 xTranslator，但输入、输出和日志必须全部位于当前项目内。

## Codex 不能做

- 不能访问真实游戏目录、真实 MO2/Vortex 目录、Steam 游戏目录、AppData 或 Documents/My Games 配置目录。
- 不能直接修改 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`、`.pex`、`.dll`、`.exe` 等二进制文件。
- 不能直接修改 `.psc` 源码并重新编译。
- 不能覆盖 `mod/` 下原始输入。
- 不能自动复制 `final_mod/` 或 `_CHS.zip` 到 MO2/Vortex。
- 不能把 GUI 只打开、只检查或人工临时保存伪装成自动化完成。

## 常规入口

```console
python .\scripts\audit_translation_readiness.py
python .\scripts\run_translation_queue.py --mode prepare --limit 1
python .\scripts\run_non_gui_translation_workflow.py --mod-name <ModName> --source-path ".\mod\<ModArchive>.zip" --force-prepare
python .\scripts\test_workflow_health.py --mod-name <ModName> --run-strict-gate
```

同一项目不要并行运行总控、严格门禁、状态刷新和健康检查入口；这些入口会使用项目内 workflow lock，避免报告和 final_mod 校验互相覆盖。

## 详细规则索引

| 主题 | 权威文档 |
|---|---|
| 翻译风格、禁翻项、占位符、Papyrus 可见文本 | `docs/translation_rules.md` |
| decoder-first、非 GUI 主流程、ESP/PEX 工具优先级 | `docs/decoder_first_workflow.md` |
| LexTranslator GUI fallback | `docs/lextranslator_workflow.md` |
| xTranslator GUI fallback | `docs/xtranslator_workflow.md` |
| GUI / Computer Use 操作边界 | `docs/gui_automation_rules.md` |
| Tool Adapter 和本地工具配置 | `docs/tool_adapter.md` |
| PEX 可见字符串写回 | `docs/pex_visible_strings_writeback.md` |
| Skill 路由、职责边界、防重复探索 | `docs/skill_architecture.md` |
| final_mod、intermediate、`_CHS.zip` 输出结构 | `docs/final_mod_output.md` |
| 模型校对、严格门禁、目标合规和玩家实机边界 | `docs/translation_proofreading_workflow.md` |

## 完成边界

项目内静态校对完成不等于玩家实机验证完成。真实游戏测试由玩家操作；玩家尚未提供真实游戏测试结果和证据时，应在目标合规报告中标记为校对工作流范围外，而不是当作项目内校对阻断。
