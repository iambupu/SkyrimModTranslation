## Context

项目已通过独立 Python 脚本实现工作区初始化、工具准备、Mod materialization、readiness、workflow state/tasks、低风险恢复、QA 和进度卡。用户和顶层 Agent 目前必须理解并组合这些内部入口；`resume_workflow.py` 还会把正常“无任务”表示为内部退出码 `2`。本变更需要在不削弱现有状态机、路径边界和 QA 的前提下增加一个薄控制门面。

运行时仅支持 Windows，但 Ubuntu static CI 会导入并 compileall `scripts/`。工作区必须位于插件仓库外，不能访问真实游戏或 Mod 管理器目录。现有 `workflow_policy.json` 的入口集合属于状态机授权面，不能把外层控制器加入其中。

完整冻结设计位于 `docs/superpowers/specs/2026-07-22-single-user-entry-cli-design.md`；本文件记录实现决策及其理由。

## Goals / Non-Goals

**Goals:**

- 用一个公开 CLI 覆盖创建/复用工作区、输入导入、工具准备、推进、恢复、状态和产物定位。
- 保持一个输入指纹、一个不可变 session、一个当前 Mod 的工作区模型。
- 让现有 workflow state/tasks 继续作为唯一权威状态，并通过稳定文本/JSON 合同向用户和 Agent 投影结果。
- 让输入变化、并发初始化、半成品导入、撕裂读取和残留子进程都可检测并安全停止。
- 保持现有底层脚本可独立测试和供开发者诊断。

**Non-Goals:**

- 不把项目改成可安装 Python package，不增加 PATH 命令。
- 不重构现有 queue、workflow、QA、adapter 或状态枚举。
- 不在 CLI 内调用模型翻译或扩展 GUI 自动化。
- 不支持单 session 多 Mod，不自动修复 doctor 发现的问题。
- 不自动安装产物到游戏、MO2 或 Vortex。

## Decisions

### 1. 公开门面与私有帮助模块

`scripts/smt.py` 只负责 argparse 与 stdout/stderr；`scripts/smt_cli.py` 是唯一内部门面并返回 `CliResult`。Win32 API 和指纹分别放入无 CLI 的 `smt_windows.py`、`smt_fingerprint.py`。这保留单一公开入口，同时避免形成新的超大脚本。

替代方案是直接导入现有 `main()` 或先抽取统一编排库。前者耦合 argparse、环境和全局状态，后者扩大回归面，因此第一版通过受监管子进程复用现有脚本。

### 2. `smt-input-v1` 与不可变 session

复合身份统一为 `smt-input-v1:<game_id>:<source_kind>:<digest>`。ZIP/7Z 使用文件 SHA-256；目录使用带 magic、version、entry type、NFC 路径长度/字节、文件大小和原始 SHA-256 的二进制合同。指纹函数返回不可变 manifest；复制后同时验证目标摘要并重新计算源目录完整 manifest/digest，避免同长度覆写并恢复 mtime 绕过身份元组检查。

`.workflow/smt-session.json` 只允许首次原子创建，后续运行只能验证。它不保存源绝对路径。Local AppData 的 `cli-state.json` 只是缓存；映射失效后重新验证直属工作区，多个匹配且缓存不能裁决时返回冲突。

替代方案是按路径或 Mod 名复用。它们无法区分内容版本、游戏 Profile 或改名副本，容易污染旧 QA 证据，因此不采用。

### 3. Reservation、事务导入与无死锁锁序

全局缓存锁只短时保护名称与 mapping。创建者在全局锁内写 reservation，并只对新 reservation 锁做非阻塞获取；任何等待都发生在释放全局锁之后。耗时初始化、工具准备和导入持 reservation/工作区独占锁，不持全局锁。提交 mapping 时允许从下层锁短时获取全局锁，因为所有反向路径禁止持全局锁等待下层锁。

工作区尚未创建时使用 Local AppData reservation 锁；初始化生成 `.workflow/` 后无缝获取 `smt-operation.lock`。锁通过延迟加载的 `LockFileEx` 实现，所有权属于文件句柄，不复用现有 stale-file 锁。共享读取者不写锁元数据。

输入先复制到 `mod/.smt-import-<uuid>.partial`，目标指纹和源 manifest 均验证后才原子改名、首次写 session、提交 mapping。合法 session 已写但 mapping 尚未提交的崩溃点可以自动补登记；没有合法 session 的 reservation 只报告并分配新名称，不覆盖旧目录。

### 4. 状态驱动而非完整流程盲跑

新输入只执行精确过滤的 queue prepare，然后按 `workflow_refresh.CORE_REFRESH_STEPS` 刷新。CLI 从 tasks 中选择当前 session Mod 的一个低风险非 GUI task，并调用 `resume_workflow.py --mod-name ... --task-id ...`。不使用缺少 task/mod 精确过滤的 `run_workflow_tasks.py --limit 1`，也不因命令名为 `run` 就盲调完整 workflow。

`classify_outcome()` 返回六种稳定 outcome 或 `None`。只要仍有合法自动任务就返回 `None`；`completed` 必须同时满足 project/current Mod `manual_tested`，`ready_for_manual_test` 也要求项目级一致且无全局 blocker。公开 outcome 不写回 workflow state。

循环使用状态摘要、`task_id + evidence`、单次 blocker 精确计数、跨命令 `last_attempt` 与摘要不变判定来停止无进展；不把通用 `retry_count` 解释成同一 blocker 次数。

### 5. 受监管子进程和稳定结果

`Popen` 输出以固定二进制块读取，使用由显式 `output_encoding` 或 Windows 系统文本编码唯一确定的单个增量解码器，解码后才写完整日志并维护 200 行尾部；不根据内容猜测编码。Windows 以 `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP` 启动，先配置并分配带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，再恢复主线程，关闭子进程创建逃逸后代的窗口；超时或 Ctrl+C 终止整棵进程树。Win32 绑定均延迟加载，使非 Windows 导入、compileall 和 `--help` 正常。

文本模式显示 outcome、原始进度卡和产物；JSON 模式 stdout 只有固定 schema v1 单对象。`ArtifactInfo` 同时表达路径、存在性、类型和验证证据。权威状态的无时区时间原样返回并以 `state_generated_at_timezone=null` 标明，不伪装成 UTC。

### 6. 外部控制器不进入 workflow 授权面

`smt.py` 不加入 `allowed_entrypoint_scripts` 或其他 `allowed_scripts()` 来源，也不能由 next actions/tasks 生成。公开入口通过 CLI 合同测试和文档/Skills 静态检查固定。这样消除 `workflow task -> smt.py -> workflow task` 的递归可能。

## Risks / Trade-offs

- [目录指纹会读取全部文件] → 使用 1 MiB 流式哈希和一次 manifest，正确性优先；只在 `run` 输入识别/复用时计算。
- [LockFileEx/Job Object 增加 Win32 复杂度] → 隔离到 `smt_windows.py`，延迟加载，并增加 Windows 多进程与后代进程测试。
- [reservation 崩溃会留下诊断记录和目录] → 不自动删除用户数据；有合法 session 时补 mapping，无 session 时分配新名称并由 doctor 报告。
- [状态/产物只读命令在写入期间可能繁忙] → 使用短时共享锁；超时返回 `busy=true`，不读取撕裂快照。
- [单 Mod session 不覆盖批量队列] → 这是降低用户和 Agent 歧义的明确边界；现有开发者队列入口继续保留。
- [文档切换可能隐藏底层调试入口] → 普通指南只展示 `smt.py`，高级和开发者指南保留内部命令并明确其非公开属性。

## Migration Plan

1. 先实现纯指纹、Win32 边界、结果类型和单元测试，不改变文档入口。
2. 实现缓存/session/reservation/事务导入和五个命令，接入精确 task 推进与效果 fixture。
3. 更新 `.gitignore` 精确放行新测试，完成 Ubuntu/Windows/效果回归。
4. 最后迁移普通用户、Agent 和 Skills 文档；底层脚本继续保留。
5. 在最新提交上进行独立 Agent 审查；任何修复提交后重新审查，并等待五类 required checks。

回滚时撤销公开 CLI 和文档迁移即可。已创建工作区、session 和 Local AppData 缓存保持可读且不删除；现有底层工作流未变，无需数据迁移或回滚脚本。

## Open Questions

无。指纹、锁序、公开 outcome、退出码、JSON schema、工具准备、文档边界和 CI 策略均已在书面规格审查中冻结。
