## 1. 测试跟踪与公开合同骨架

- [x] 1.1 将 `.gitignore` 的 `tests/` 规则改为精确 allowlist，只解除 `tests/test_smt_cli.py` 与 `tests/test_smt_cli_workspace.py`，确认其他本地测试仍被忽略
- [x] 1.2 在 `scripts/smt_cli.py` 定义 `ArtifactInfo`、`CliResult`、公开 outcome/退出码和固定 JSON schema，并以单元测试证明模块不写 stdout/stderr
- [x] 1.3 在 `scripts/smt.py` 建立五个子命令、全局 `--format` 和仅由该模块负责的 text/JSON 渲染，验证 argparse 参数错误仍为 `2`
- [x] 1.4 添加非 Windows import/compileall/`--help` 测试，并验证真实子命令返回环境不可用 `5`

## 2. 输入指纹与路径身份

- [x] 2.1 在 `scripts/smt_fingerprint.py` 定义不可变 `InputEntry`/`InputManifest` 和 `smt-input-v1` 复合身份
- [x] 2.2 实现 ZIP/7Z 普通文件校验、1 MiB 流式 SHA-256 和哈希/复制前后文件身份变化检测
- [x] 2.3 实现目录二进制指纹合同，包括 NFC POSIX 路径、空目录、稳定排序、Windows 大小写冲突和现有 `discover_regular_tree()` 安全检查
- [x] 2.4 实现目标摘要验证与源目录/归档完整重新哈希，并补齐同长度覆写后恢复 mtime、新增/删除/重命名/类型变化及归档变化回归测试
- [x] 2.5 实现基于 `safe_file_name()` 的 Mod/导入/工作区名称和 80 UTF-16 code unit 限制测试

## 3. Windows 平台边界

- [x] 3.1 在 `scripts/smt_windows.py` 延迟实现 Documents/Local AppData Known Folder，验证不得静默回退到猜测路径
- [x] 3.2 实现基于 `LockFileEx` 的 `SmtProcessFileLock` 共享/独占模式、超时和只由独占持有者写诊断元数据
- [x] 3.3 实现 `CREATE_SUSPENDED` 后先分配 Job 再恢复线程的 Process Group/Job Object、`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`、CTRL_BREAK 和 taskkill 兜底监管
- [x] 3.4 添加 Windows 多进程锁、不同工作区并行和超时/Ctrl+C 无残留后代进程测试

## 4. 工作区缓存、Session 与事务导入

- [x] 4.1 实现 `cli-state.json` 原子缓存、最近工作区、复合身份 mapping 和 reservation schema，确保缓存始终非权威
- [x] 4.2 实现 run/其他命令不同的工作区寻址顺序、显式工作区冲突和默认根目录直属 session 扫描
- [x] 4.3 实现不可变 `smt-session.json` 首次原子创建与完整验证，禁止普通 run 覆盖或迁移身份字段
- [x] 4.4 实现无死锁两阶段 reservation，保证持有全局锁时不等待下层锁，并支持相同输入等待与不同输入并行
- [x] 4.5 实现 `.partial` 事务复制、目标/源双重验证、原子提交和仅由 CLI 拥有工作区写入的失败报告
- [x] 4.6 实现 session 已提交但 mapping 缺失的自动补登记、多匹配冲突和无 session reservation 的保留/新命名规则
- [x] 4.7 实现单 session 额外 Mod 输入检测，并验证 queue 参数始终精确包含 mod-name、source-path 和 limit 1

## 5. 状态投影与精确推进

- [x] 5.1 实现 CORE refresh 复用和权威 state/tasks/progress card 读取，不在 CLI 维护第二套刷新列表
- [x] 5.2 实现当前 Mod 精确低风险非 GUI 任务选择，并通过 `resume_workflow.py --mod-name --task-id` 执行
- [x] 5.3 实现返回 `PublicOutcome | None` 的分类器，覆盖自动任务优先、GUI/Agent/用户暂停和普通 blocked
- [x] 5.4 实现 completed/ready 的 project/current Mod 一致性和 global/project blocker 判定
- [x] 5.5 实现状态摘要、单次 blocker+evidence 两次上限、跨命令 last_attempt 无变化停止和 max steps/超时
- [x] 5.6 实现增量子进程日志、200 行尾部、底层退出码诊断和超时/中断公开结果映射

## 6. 五个公开命令

- [x] 6.1 完成 `run` 的输入识别、工作区复用/初始化、tool-setup 语义、事务导入、精确 queue prepare 和状态驱动推进
- [x] 6.2 完成 `resume` 的独占锁、精确任务循环和内部无任务退出码 `2` 到公开 no-op `0` 的归一化
- [x] 6.3 完成 `status` 的短时共享锁、只读快照、原始 progress card 和 busy/缺失/身份错误语义
- [x] 6.4 完成纯诊断 `doctor`，验证它不安装、清理、刷新、修改 session/cache/tools 或认领工作区
- [x] 6.5 完成 `output` 的 ArtifactInfo、人工测试双状态和四个预定义 `--open` 目标
- [x] 6.6 完成 text 与 JSON schema v1 端到端测试，确保 stdout 单对象、字段恒定和无时区时间原样返回

## 7. 文档、Skills 与效果回归

- [x] 7.1 更新 `README.md` 和 `USER_GUIDE.md`，让普通用户只看到五个公开命令和默认 Documents 工作区
- [x] 7.2 更新高级/开发者指南与 `scripts/README.md`，把现有底层入口明确标记为内部诊断/实现接口
- [x] 7.3 更新 `AGENTS.md` 及用户入口、运行期编排、恢复 Skills，使顶层 Agent 只调用 `smt.py --format json`，同时不把 smt.py 加入 workflow policy 授权集合
- [x] 7.4 增加静态合同测试，阻止普通用户/顶层 Agent 文档重新组合底层脚本或 workflow task 指向 smt.py
- [ ] 7.5 增加 `--tool-setup skip` 的安全 ZIP 效果 fixture，验证首次暂停、相同输入复用和内容变化新工作区
- [ ] 7.6 使用 stub 初始化/工具 runner 完成 reservation/并发/session 集成测试，确保 CI 不下载真实 Python/.NET/外部工具

## 8. 合入验证与独立审查

- [ ] 8.1 运行 SMT 定向单元、工作区、Windows 并发和平台兼容测试，并保存失败修复记录
- [ ] 8.2 运行 static、windows-smoke、windows-fallout4-adapters、windows-fallout4-workflow 和 effect-regression 五类 required checks
- [ ] 8.3 在最新提交上进行独立 Agent 高风险审查并记录 reviewed commit；任何修复提交后重新审查
- [ ] 8.4 确认工作树只包含本变更文件、OpenSpec 验证通过、文档/JSON/退出码合同一致后再提交合并
