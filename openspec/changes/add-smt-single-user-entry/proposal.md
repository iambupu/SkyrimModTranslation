## Why

当前用户和顶层 Agent 必须自行组合初始化、输入导入、工具准备、队列、恢复、状态查询和产物定位脚本。底层状态机和 QA 能力已经存在，但分散入口增加了学习成本、错误脚本选择、状态刷新遗漏和内部退出码误判的风险，因此需要在不重构现有工作流的前提下建立唯一公开控制入口。

## What Changes

- 新增 `python scripts/smt.py`，提供 `run`、`status`、`resume`、`doctor` 和 `output` 五个公开子命令，并同时提供文本和 schema v1 JSON 结果。
- 在 Windows Documents 下自动分配单 Mod 工作区，以 `smt-input-v1` 复合身份复用相同输入；使用不可变 session、缓存非权威映射、reservation 和事务导入防止串包与半成品。
- 使用独立 Win32 文件锁和 Job Object 保护并发状态、共享读取和子进程树；非 Windows 环境仍可导入、编译和显示帮助。
- 复用现有 readiness、workflow state/tasks、canonical refresh 和 `resume_workflow.py --task-id`，把现有状态投影为公开 outcome，不增加新的 workflow state。
- 将普通用户和顶层 Agent 文档迁移到唯一入口；底层脚本继续作为内部实现和开发诊断入口。
- 新增纯逻辑、事务工作区、Windows 并发、平台兼容和效果回归测试；精确解除两个新测试文件的 `.gitignore` 忽略。
- `smt.py` 不加入 `workflow_policy.json` 的 `allowed_entrypoint_scripts` 或其他状态机授权集合，避免递归控制。

## Capabilities

### New Capabilities

- `smt-public-cli`: 定义五个公开子命令、工作区寻址、文本/JSON 输出、公开退出码、doctor/output 行为和 Agent 使用合同。
- `smt-input-session`: 定义 `smt-input-v1` 指纹、不可变 session、缓存非权威映射、reservation、Win32 锁和事务导入安全合同。
- `smt-workflow-projection`: 定义精确单 Mod 任务选择、状态驱动推进、无进展停止、现有状态到公开 outcome 的投影和子进程监管。

### Modified Capabilities

无。现有 Bethesda 字符串表、PEX、Light FormKey 和 localized delivery 规格的行为要求不变。

## Impact

- 新增公开/私有脚本：`scripts/smt.py`、`scripts/smt_cli.py`、`scripts/smt_windows.py`、`scripts/smt_fingerprint.py`。
- 新增测试：`tests/test_smt_cli.py`、`tests/test_smt_cli_workspace.py`，并精确调整 `.gitignore`。
- 更新 `README.md`、用户/开发者指南、`AGENTS.md`、相关入口/编排/恢复 Skills 和静态合同检查。
- 读取 Windows Known Folder，在 Local AppData 保存可丢弃 CLI 缓存、reservation、锁和初始化期日志；在工作区 `.workflow/` 保存不可变 session、操作锁和运行日志。
- 继续调用现有初始化、queue prepare、canonical refresh、task 和 QA 脚本；不改变 `pyproject.toml` 的 `package=false`，不新增外部运行时依赖，不访问真实游戏或 Mod 管理器目录。
