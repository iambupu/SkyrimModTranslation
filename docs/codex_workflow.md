# Codex Workflow

本文件只作为 Codex 接手入口和文档索引，不重复展开 final_mod、翻译规则或校对门禁细节。

## 控制分层

- Codex 负责准确和灵活的编排：阅读状态、解释阻断、选择下一步、决定是否重试或停下。
- 状态机负责边界和证据：记录当前阶段、最后成功阶段、允许动作、推荐动作、修复候选和停止条件。
- 脚本负责可复现动作：只执行项目内 Python 入口，生成可重跑、可审计的中间产物和报告。
- QA 负责是否允许推进：严格门禁、覆盖率、结构校验、模型审读和 final_mod 校验决定状态能否前进。

## 默认接手顺序

1. 先读 `AGENTS.md`，确认项目边界和禁止事项。
2. 再读 `qa/workflow_state.md` 或 `qa/workflow_state.json`，确认每个 Mod 当前状态、最后成功阶段、阻断检查、`recommended_actions`、`repair_candidates`、`stop_conditions` 和下一条建议命令。
3. 再读 `qa/workflow_health.md` 或 `qa/workflow_health.json`，确认核心脚本、Skill、严格门禁和最终证据状态。
4. 再读 `qa/translation_readiness.md` 或 `qa/translation_readiness.json`，确认 `mod/` 输入、已知输出和项目级状态。
5. 如果 workflow state 给出推荐命令，优先执行推荐命令；不要手动拼接分步脚本。
6. 如果状态是 `qa_failed` 或 `blocked`，使用 `workflow-agent-orchestration`：先读阻断报告，再选择一个允许动作，执行前后写入 `qa/workflow_agent_runs.jsonl`。
7. 只有状态 blocked、证据缺失或用户明确要求局部处理时，才打开具体规则文档和对应 Skill。

`qa/workflow_state.json` 的 `allowed_scripts` 已合并 `workflow_policy.json` 中的常规状态刷新脚本、总控入口脚本、当前阶段脚本和 QA/adapter 分步脚本。Codex 可以灵活选择其中一个动作，但不能把未授权脚本当成推荐命令执行。

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
python .\scripts\write_workflow_state.py
python .\scripts\run_translation_queue.py --mode prepare --limit 1
python .\scripts\run_non_gui_translation_workflow.py --mod-name <ModName> --source-path ".\mod\<ModArchive>.zip" --force-prepare
python .\scripts\test_workflow_health.py --mod-name <ModName> --run-strict-gate
```

同一项目不要并行运行总控、严格门禁、状态刷新和健康检查入口；这些入口会使用项目内 workflow lock，避免报告和 final_mod 校验互相覆盖。

## 详细规则索引

| 主题 | 权威文档 |
|---|---|
| 翻译风格、禁翻项、占位符、Papyrus 可见文本 | `docs/translation_rules.md` |
| LexTranslator 风格动态词典、RAG 索引、mtime 刷新规则 | `docs/lextranslator_dictionary_rag.md` |
| decoder-first、非 GUI 主流程、ESP/PEX 工具优先级 | `docs/decoder_first_workflow.md` |
| BSA 只读审计、安全解包、归档 manifest 和 loose override 交付边界 | `.codex/skills/bsa-archive-audit/SKILL.md` |
| LexTranslator GUI fallback | `docs/lextranslator_workflow.md` |
| xTranslator GUI fallback | `docs/xtranslator_workflow.md` |
| GUI / Computer Use 操作边界 | `docs/gui_automation_rules.md` |
| Tool Adapter 和本地工具配置 | `docs/tool_adapter.md` |
| PEX 可见字符串写回 | `docs/pex_visible_strings_writeback.md` |
| Skill 路由、职责边界、防重复探索 | `docs/skill_architecture.md` |
| 状态机、允许动作和下一步命令 | `config/workflow_policy.json` / `qa/workflow_state.json` |
| Codex 轻量编排、恢复尝试和重试日志 | `.codex/skills/workflow-agent-orchestration/SKILL.md` / `qa/workflow_agent_runs.jsonl` |
| final_mod、intermediate、`_CHS.zip` 输出结构 | `docs/final_mod_output.md` |
| 模型校对、严格门禁、目标合规和玩家实机边界 | `docs/translation_proofreading_workflow.md` |

## 完成边界

项目内静态校对完成不等于玩家实机验证完成。真实游戏测试由玩家操作；玩家尚未提供真实游戏测试结果和证据时，应在目标合规报告中标记为校对工作流范围外，而不是当作项目内校对阻断。
