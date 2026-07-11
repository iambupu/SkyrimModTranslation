# 全自动效果回归验证测试流程

本文定义项目内可全自动执行的“效果回归验证”流程。这里的“效果”指插件源仓库和工作区脚本对可控输入产生的可观察交付结果是否保持稳定，包括 `final_mod/` 文件、覆盖率、provenance、严格 QA 报告、进度卡和接手报告。它不等同于真实游戏内效果验证。

真实 Skyrim、MO2、Vortex、Steam、AppData、`Documents/My Games`、LexTranslator/xTranslator GUI、LLM API 和外部翻译 API 不属于全自动回归测试输入，也不得在 CI 中调用。

## 目标

全自动回归测试要回答这些问题：

| 问题 | 自动验证方式 |
|---|---|
| 仓库结构是否仍可作为 Codex 插件加载 | `scripts/ci_validate_repo.py --strict` |
| runtime/meta Skill 是否仍可被 Codex 正确选择 | Skill frontmatter、重复 name、runtime/meta 分离检查 |
| workflow policy 是否仍指向存在的 Python 入口 | `workflow_policy.json` 脚本引用检查 |
| Python 脚本是否仍可在干净 CI 环境导入/编译 | `python -m compileall scripts` |
| 固定 fixture 输入是否生成相同类别的输出 | fixture workspace 运行后比较规范化快照 |
| final_mod 是否保持直接替换、hash 和 provenance 规则 | `validate_final_mod.py` 与快照比对 |
| QA 报告链是否没有过期或缺失 | strict gate、readiness、workflow state、health、goal compliance 快照比对 |
| 玩家实机测试边界是否仍被保留 | manual plan/template 生成且不标记为自动完成 |

## 非目标

这些内容不能由全自动项目内回归测试证明：

- 游戏真实加载成功。
- MCM 页面实际显示。
- 任务、对话、法术、脚本事件在游戏内运行正确。
- 玩家可感知的翻译质量已经完全达标。
- LexTranslator/xTranslator GUI 可以在任意机器上稳定保存。
- 外部翻译、LLM 或联网服务可用。

全自动回归通过时，只能说明“项目内可复现输出没有回归，可以继续进入人工游戏内测试或人工语义审查”。

## 测试资产布局

后续新增测试资产时，优先放在可提交的轻量目录，不依赖 ignored 的工作区输出目录。

```text
samples/effect_regression/
  cases/
    <case-name>/
      input/
        mod/
        glossary/
        config/
      expected/
        manifest.json
        file_hashes.json
        qa_metrics.json
        workflow_state.json
        progress_card.md
        report_index.json
```

约束：

- fixture 输入必须是项目可提交的小型样本，不包含真实 Mod 二进制、真实游戏文件、用户私有配置或压缩包。
- 如果必须覆盖 ESP/PEX/BSA 相关逻辑，优先使用脚本级单元样本、反读 packet 样本、manifest 样本和受控 tool output 样本；不要把真实 `.esp/.pex/.bsa` 提交进仓库。
- `expected/` 只保存规范化后的稳定结果，不保存本机绝对路径、时间戳、临时目录、随机文件名或机器相关工具路径。
- `tests/` 当前作为本地/临时测试目录被 `.gitignore` 忽略；需要持久化的回归验证应放在受跟踪的 `scripts/`、`samples/effect_regression/`、文档化 fixture 或 CI 入口中。

## 规范化规则

回归快照必须先规范化再比较，避免把机器差异当成回归。

必须去除或替换：

- 绝对路径。
- 临时目录。
- 运行时间戳。
- 本机工具路径。
- 工作区随机目录名。
- 文件系统换行差异。

必须保留：

- 相对路径。
- 文件 SHA256。
- QA 阻断和警告数量。
- coverage missing/unverified 数量。
- provenance 行数和 hash mismatch 数量。
- strict-complete 是否为 true。
- progress card 用户可见状态、下一步、阻断原因。
- manual test plan/template 是否绑定当前包 hash 和 manifest hash。

## 全自动执行阶段

### Stage 0: 仓库结构门禁

目的：阻止插件结构、Skill 元数据、workflow policy 和 Python 入口被改坏。

入口：

```console
python scripts/ci_validate_repo.py --strict
python -m compileall scripts
python scripts/test_workflow_health.py --repo-only --strict
```

运行位置：

- 每次 push / pull request。
- Windows 和 Ubuntu 都可以运行。
- 不需要外部工具。

### Stage 1: 轻量脚本级回归

目的：验证核心脚本对小型文本样本的行为没有回归。

建议覆盖：

- JSON/JSONL 解析和字段保护。
- Interface 翻译文件结构保护。
- placeholder/control token 检查。
- final text/binary review packet 的解析逻辑。
- model review contract 复用逻辑。
- workflow progress/task 派生逻辑。
- subprocess 文本解码。

当前可运行入口：

```console
python scripts/test_workflow_task_parallelism.py
```

前提：

- `tests/` 下的测试只作为本地临时验证，不作为 Git 可跟踪源码。
- 需要保留的覆盖应迁移到受跟踪脚本、`samples/effect_regression/` fixture 或 CI 校验入口；当前 workflow task 并发覆盖位于 `scripts/test_workflow_task_parallelism.py`。
- 本地临时测试不得依赖真实工具安装状态。
- 本地临时测试不得写入 `mod/`、`out/`、`qa/`、`work/` 等真实工作区目录；应使用临时目录。

### Stage 2: Fixture workspace 回归

目的：验证从可控输入到工作区输出的端到端报告链没有回归。

建议新增 Python 入口：

```console
python scripts/run_effect_regression.py --case <case-name> --ci
python scripts/run_effect_regression.py --all --ci
```

运行步骤：

1. 创建临时工作区，例如系统临时目录下的 `skyrim-chs-effect-regression/<case-name>/`。
2. 复制 `samples/effect_regression/cases/<case-name>/input/` 到临时工作区。
3. 写入 `.skyrim-chs-workspace.json`，指向当前插件源仓库。
4. 使用 `--tool-setup skip` 或 repo-only 配置，禁止外部工具探测影响结论。
5. 只运行该 case 授权的 Python 脚本，不运行 GUI、LLM 或联网 API。
6. 生成 `final_mod/`、QA 报告、workflow state、progress card 和 handoff 报告。
7. 规范化输出。
8. 与 `expected/` 快照比较。
9. 删除临时工作区，或在失败时按 `--keep-failed-workspace` 保留到项目内 `.tmp/` 供排查。

Stage 2 不应直接复用开发者本机已有 `work/`、`qa/` 或 `out/`，否则会把历史产物和当前变更混在一起。

### Stage 3: 严格 QA 报告链回归

目的：验证项目内 ready 判定没有被弱化。

每个 fixture case 至少比较这些指标：

| 报告 | 必须断言 |
|---|---|
| `qa/<ModName>.non_gui_qa_gates.md` | strict-complete 为 true，blocking/warnings 为 0，或 case 明确期望 blocked |
| `qa/final_mod_validation.md` | missing provenance、final hash mismatch、source hash mismatch 为 0 |
| `qa/<ModName>.final_text_structure.md` | blocking/warnings 为 0 |
| `qa/<ModName>.final_review_quality.md` | blocking/warnings 为 0 |
| `qa/translation_readiness.json` | 状态与 case 期望一致 |
| `qa/workflow_state.json` | state、last_success_stage、next_actions 与期望一致 |
| `.workflow/progress_card.md` | 用户可见状态与 workflow state 一致 |
| `qa/workflow_health.json` | BlockingIssues 为 0，KnownOutputs 范围正确 |
| `qa/project_completion_audit.json` | 覆盖全部 known outputs |
| `qa/translation_goal_compliance.json` | 项目内 QA 和玩家实机边界区分正确 |

如果 case 设计为负向测试，期望应该是明确 blocked 或 qa_failed，而不是让脚本异常退出或产生不稳定结果。

### Stage 4: Manual test artifact contract 回归

目的：验证“人工游戏内测试不能被自动完成”这个边界没有被破坏。

自动检查：

- `new_manual_game_test_plan.py` 能为 ready 输出生成计划。
- `new_manual_game_test_results_template.py` 绑定当前 CHS 包 SHA256 和 final_mod manifest SHA256。
- 空泛证据、旧包 hash、旧 manifest hash、计划外 Mod、缺失附件等输入会被 `validate_manual_game_test_results.py` 拒绝。
- `audit_translation_goal_compliance.py` 在没有玩家证据时，把玩家实机验证标为外部后续证据，而不是项目内 QA 阻断。

禁止：

- 在 CI 中自动写“已通过游戏测试”。
- 把截图、日志或手写 `ok` 当作自动通过。
- 访问真实游戏、MO2/Vortex 或 AppData 路径。

## CI 分层建议

当前 CI 默认在 push / pull request 和 `workflow_dispatch` 上运行 Stage 0 与第一版 `effect-regression` fixture job。下表把当前已启用 job 和规划项分开标注：

| CI job | 触发 | 内容 | 外部依赖 |
|---|---|---|---|
| `static` | push/PR | Stage 0 repo validation + Stage 1 workflow task parallelism tests | 无 |
| `windows-smoke` | push/PR | Stage 0 Windows repo-only smoke + Stage 1 workflow task parallelism tests | 无 |
| `script-regression` | 规划：push/PR | 后续更多小型脚本级测试 | 无 |
| `effect-regression` | push / pull_request / workflow_dispatch | Stage 2 到 Stage 4 fixture workspace 回归 | 无 |
| `nightly-effect-regression` | 规划：schedule/workflow_dispatch | 更多 fixture case，失败保留 artifact | 无 |

`effect-regression` job 已进入普通 push / pull request 门禁；新增 fixture 必须保持可重复、无 GUI、无外部游戏目录依赖。

当前已提供的第一版 case：

| Case | 覆盖内容 |
|---|---|
| `repo-contract` | 运行 Stage 0 三个命令，并比对插件名、Skill 数量、workflow policy 脚本引用数量、wrapper 禁止项和手动测试边界标志 |

## Case 设计建议

第一批 fixture case 建议从低风险、纯文本、可稳定比较的场景开始：

| Case | 覆盖内容 |
|---|---|
| `interface-basic` | `Interface/translations/*.txt` key/tab/UTF-16 LE BOM/final_mod 同路径覆盖 |
| `json-structured-text` | JSON key 保护、值翻译、final text structure |
| `mcm-visible-text` | MCM 页面标题、帮助文本、运行时 key 保护 |
| `final-mod-provenance` | final_mod manifest、provenance、CHS 包一致性 |
| `manual-test-contract` | manual plan/template/result validator 的拒绝和接受路径 |
| `qa-negative-placeholder` | 占位符丢失时必须 blocked |
| `qa-negative-sidecar` | 旁挂语言文件不能作为直接替换交付 |

ESP/PEX/BSA 相关 case 可以第二批加入，优先用受控导出文本、manifest 和 tool output 样本模拟，不提交真实二进制。

## Golden 快照更新规则

快照更新必须是显式动作，不能由普通 CI 自动改写。

建议入口：

```console
python scripts/run_effect_regression.py --case <case-name> --update-expected
```

更新前必须确认：

- 变更是预期行为变化，不是 QA 弱化。
- strict gate 仍干净。
- final_mod hash/provenance 变化可解释。
- progress card 文案和状态变化符合 `workflow_policy.json`。
- manual game test boundary 没有被改成自动完成。

快照 PR 需要在说明里列出每个 case 的差异摘要。

## 失败分流

| 失败类型 | 归因方向 |
|---|---|
| repo validation 失败 | 插件结构、Skill、workflow policy 或 Python 编译问题 |
| fixture 输出 hash 变化 | 构建逻辑、文件复制、编码、provenance 或规范化规则变化 |
| QA metric 变化 | 覆盖率、strict gate、final review quality 或 readiness 判定变化 |
| progress card 变化 | workflow state 派生或用户进度输出合同变化 |
| manual boundary 变化 | 人工游戏测试边界被弱化或模板绑定失效 |
| 仅绝对路径/时间戳变化 | 规范化规则不足 |

修复后必须重新运行同一 case，直到输出稳定。

## 完成标准

某次全自动效果回归验证可以标记通过，必须同时满足：

- Stage 0 通过。
- 该次要求覆盖的所有 Stage 1 测试通过。
- 所有被选中的 fixture case 运行完成。
- 规范化快照无未解释差异。
- 所有 QA 指标符合 case 期望。
- 没有访问真实游戏、MO2/Vortex、Steam、AppData、用户 Documents 或外部 API。
- 没有把 manual game test 标记为自动完成。
- 失败 case 至少重跑一次确认不是临时文件或并发问题。

只有这些证据齐全时，才能说“项目内全自动效果回归验证通过”。真实游戏效果仍由 `qa/manual_game_test_plan.md` 和玩家提交的 `qa/manual_game_test_results.json` 证据链验证。
