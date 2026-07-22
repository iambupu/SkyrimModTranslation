## ADDED Requirements

### Requirement: 单一公开命令面
系统 SHALL 以 `python scripts/smt.py` 作为普通用户和顶层 Agent 的唯一公开控制入口，并提供 `run`、`status`、`resume`、`doctor`、`output` 五个子命令。现有底层脚本 MUST 保留为内部实现或开发者诊断入口，`smt.py` MUST NOT 加入 workflow policy 的授权入口集合。

#### Scenario: 用户启动一个新翻译任务
- **WHEN** 用户执行 `python scripts/smt.py run <input> --game skyrim-se`
- **THEN** 系统通过公开门面编排现有内部脚本，而不要求用户组合初始化、queue、刷新或恢复命令

#### Scenario: 状态机生成下一任务
- **WHEN** workflow state/tasks 生成下一动作
- **THEN** 该动作 MUST NOT 指向 `scripts/smt.py`，避免外层控制器递归调用自身

### Requirement: 公开命令可加载性
CLI MUST 保持 `[tool.uv] package = false`，MUST NOT 要求 pip/PATH 安装，并 MUST 延迟加载 Windows API，使非 Windows 环境可以 compileall、导入模块和显示帮助。

#### Scenario: Ubuntu 静态检查显示帮助
- **WHEN** CI 在非 Windows 环境执行 `python scripts/smt.py --help`
- **THEN** 命令成功显示帮助，且不得因 `ctypes.windll`、Known Folder 或 Job Object 导入失败

#### Scenario: 非 Windows 执行真实命令
- **WHEN** 非 Windows 环境执行 `run`、`resume`、`status`、`doctor` 或 `output`
- **THEN** CLI 返回环境不可用退出码 `5`，而不是模块导入异常

### Requirement: 命令工作区寻址
`run` MUST 按“显式工作区、身份匹配的当前目录工作区、输入复合身份映射、新工作区”顺序解析目标。其他子命令 MUST 按“显式工作区、当前目录工作区、最近活动工作区”顺序解析；`doctor` MUST 支持在没有选定工作区时只读扫描默认根目录的直属子目录。

#### Scenario: 从无关旧工作区运行新输入
- **WHEN** 用户在工作区 A 中运行输入 B，未显式传 `--workspace`，且 A 的 session 与 B 不匹配
- **THEN** `run` 忽略 A 并继续查询 B 的映射或创建新工作区

#### Scenario: 显式工作区身份冲突
- **WHEN** 用户传入非空非工作区目录，或传入 session/marker/输入身份不匹配的工作区
- **THEN** CLI 返回退出码 `6`，且 MUST NOT 清空、覆盖或重新绑定该目录

#### Scenario: 无参数状态查询
- **WHEN** 当前目录不在工作区内且存在最近活动工作区
- **THEN** `status` 读取最近活动工作区

### Requirement: 文本和 JSON 结果合同
CLI SHALL 提供全局 `--format text|json`。`smt_cli.py` 及其私有帮助模块 MUST NOT 向 stdout/stderr 输出；只有 `smt.py` 可以渲染结果。JSON 模式 stdout MUST 只包含一个 schema v1 对象，字段结构 MUST 稳定，缺失值 MUST 使用 `null`、空数组或空对象。

#### Scenario: Agent 请求 JSON
- **WHEN** Agent 使用 `--format json` 调用任一子命令
- **THEN** stdout 只包含一个 JSON 对象，其中始终包含 command、outcome、exit_code、workspace、mod_name、game_id、workflow_state、snapshot 元数据、next_action、progress card、output_paths、details、diagnostics、日志路径和底层退出码

#### Scenario: 输出产物元数据
- **WHEN** JSON 结果包含 final_mod、intermediate 或 CHS 包
- **THEN** 每个 `ArtifactInfo` 同时包含 path、exists、kind、validated 和 validation_evidence

#### Scenario: 权威时间没有时区
- **WHEN** workflow state 的生成时间是不带时区的本地字符串
- **THEN** CLI 原样返回该字符串并设置 `state_generated_at_timezone=null`，不得标记或改写为 UTC

### Requirement: 公开退出码
CLI MUST 保留 argparse 参数错误 `2`，并 SHALL 使用 `0` 表示成功/完成/ready/no-op，`3` 表示 Agent/GUI/用户/普通安全暂停，`4` 表示输入或资源能力不支持，`5` 表示工具或运行环境不可用，`6` 表示工作区身份冲突，`124` 表示超时，`130` 表示用户中断，`1` 表示其他内部或读取失败。底层退出码 MUST 仅进入诊断字段。

#### Scenario: resume 没有任务
- **WHEN** 内部 `resume_workflow.py` 返回“没有可执行任务”的退出码 `2`，且刷新后不存在需要安全暂停的任务
- **THEN** 公开 `resume` 返回成功 no-op 和退出码 `0`，同时保留底层 `2` 到 diagnostics

#### Scenario: 状态快照显示阻断
- **WHEN** `status` 成功读取到 blocked 或 qa_failed 快照
- **THEN** `status` 退出码仍为 `0`，并通过 outcome/进度卡表达状态

#### Scenario: 能力不支持
- **WHEN** outcome 为 `blocked` 且直接原因是 Profile 或资源能力不支持
- **THEN** CLI 返回退出码 `4`，证明 outcome 与退出码不是一对一映射

### Requirement: doctor 保持只诊断
`doctor` MUST NOT 安装工具、重建 adapter、清理 partial/reservation、删除映射、修改工具配置、刷新 workflow 状态、修改 session 或自动认领工作区。它 MUST 仅执行系统/工作区读取、只读检查和 CLI 自有诊断日志写入。

#### Scenario: doctor 发现失效映射
- **WHEN** `doctor` 发现 cache 指向不存在或身份无效的工作区
- **THEN** 它报告问题但不修改 cache 或工作区

#### Scenario: doctor 检查忙碌工作区
- **WHEN** `doctor` 无法取得某工作区的短时共享锁
- **THEN** 它记录 `busy` 诊断，不绕过锁读取半写状态，也不修改该工作区

### Requirement: output 区分 QA 与人工测试
`output` MUST 使用 session 的精确 Mod 名显示 final_mod、intermediate、CHS 包及关键 QA/provenance 路径，并分别报告“可以进入人工游戏测试”和“人工游戏测试已验证”。产物不存在 MUST NOT 令只读查询失败。

#### Scenario: 产物尚未生成
- **WHEN** 工作区有效但 final_mod 或 CHS 包不存在
- **THEN** `output` 返回退出码 `0`，并将对应 ArtifactInfo 的 exists 标为 false

#### Scenario: 打开预定义目标
- **WHEN** 用户使用 `output --open final-mod` 且目录存在并位于工作区内
- **THEN** CLI 在共享锁内验证路径、释放锁后打开目录，并且不访问游戏或 Mod 管理器目录

#### Scenario: 打开非法目标
- **WHEN** 用户请求非预定义目标或目标不存在/越出工作区
- **THEN** `output` 返回退出码 `1` 且不执行系统打开

### Requirement: 顶层 Agent 使用唯一入口
顶层 Agent MUST 首次调用 `run --format json`，根据 next_action 执行语言或获授权 GUI 工作，后续仅调用 `resume/status/output --format json`。Agent MUST NOT 自行组合初始化、queue、canonical refresh、任务领取和 QA 脚本。

#### Scenario: workflow 需要 Agent 翻译
- **WHEN** `run` 返回 `needs_agent_translation` 和候选 artifacts
- **THEN** Agent 处理指定译文后调用公开 `resume --format json`，而不是自行选择底层恢复脚本
