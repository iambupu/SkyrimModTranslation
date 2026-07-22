## ADDED Requirements

### Requirement: 支持且安全的输入类型
`run` SHALL 接受普通目录、ZIP 和 7Z，MUST 拒绝 RAR、单独的插件/游戏归档顶层输入、symlink、junction、reparse point、非普通文件、多硬链接文件及现有路径安全策略禁止的位置。导入只能复制用户明确指定的输入，MUST NOT 移动或修改源。

#### Scenario: 导入安全 ZIP
- **WHEN** 用户指定一个位于普通 Mod 目录的常规 ZIP 文件
- **THEN** CLI 计算指纹并把原样副本事务导入工作区 `mod/`

#### Scenario: 输入是 reparse point
- **WHEN** 用户指定的归档或目录本身、子目录或文件包含 symlink、junction 或 reparse point
- **THEN** CLI 在复制前拒绝输入并返回不支持/安全错误，且不跟随目标

#### Scenario: 输入来自真实游戏目录
- **WHEN** 输入路径命中当前 Profile 的真实游戏、MO2 或 Vortex 风险路径
- **THEN** CLI 拒绝读取或导入该路径

### Requirement: 统一的 smt-input-v1 身份
输入复合身份 MUST 使用 `smt-input-v1:<game_id>:<source_kind>:<digest>`。ZIP/7Z digest SHALL 为归档 SHA-256；目录 digest SHALL 使用 `SMT-INPUT-DIR\0` version 1 二进制 entry 合同。游戏、来源类型或算法版本不同 MUST NOT 共享身份。

#### Scenario: 相同归档用于不同游戏
- **WHEN** 同一 SHA-256 的 ZIP 分别以 `skyrim-se` 与 `fallout4` 运行
- **THEN** 系统生成不同复合身份并不得复用工作区

#### Scenario: 目录包含空目录和 Unicode 路径
- **WHEN** 目录包含空目录、NFC/NFD 可归一化名称和普通文件
- **THEN** 指纹包含空目录、使用 NFC POSIX 相对路径并按规范化 UTF-8 字节稳定排序

#### Scenario: Windows 大小写路径冲突
- **WHEN** 目录同时包含 Windows case-insensitive key 相同的路径
- **THEN** CLI 拒绝输入，而不是生成依赖枚举顺序的摘要

### Requirement: 不可变输入 manifest 和变化检测
指纹计算 SHALL 返回 `InputManifest`，记录稳定 entries 和 digest。每个文件 SHA-256 前后 MUST 验证 `(st_dev, st_ino, st_size, st_mtime_ns)`。根目录与每个目录 entry MUST 保留 no-follow identity，并 MUST 在遍历前、`scandir` 后和 manifest 生成末尾验证未被替换；目录 identity 不进入 digest。复制后 MUST 验证目标完整摘要，并 MUST 重新计算源目录全部文件 SHA-256 及完整 manifest/digest；仅重新枚举身份元组不能代替最终内容校验。归档目标验证后也 MUST 重新计算源归档 SHA-256 并验证身份。

#### Scenario: 已发现目录在遍历前被替换
- **WHEN** 普通子目录被发现后、进入该目录前被替换为 symlink、junction 或 reparse point
- **THEN** CLI 根据绑定 identity 拒绝输入，不得把外部目标作为普通目录 entry 接受

#### Scenario: 哈希后源目录新增文件
- **WHEN** 初始 manifest 生成后、导入提交前，源目录新增文件
- **THEN** 提交前重新计算的源完整 manifest/digest 不匹配，事务失败且 session/mapping 不提交

#### Scenario: 同长度覆写后恢复时间戳
- **WHEN** 初始 manifest 生成后，源文件被同长度内容覆写且 mtime 被恢复为原值
- **THEN** 提交前重新计算的源 SHA-256/digest 与初始 manifest 不同，事务失败且 session/mapping 不提交

#### Scenario: 复制期间修改归档
- **WHEN** 归档在初始哈希或复制验证期间改变大小、时间或文件身份
- **THEN** CLI 停止导入，删除本次 staging，并不登记工作区映射

### Requirement: Documents 工作区和确定性命名
CLI SHALL 通过延迟加载的 Windows Known Folder API 获取 Documents 与 Local AppData，MUST NOT 猜测 `Path.home()/Documents` 或只信任环境变量。默认根目录 SHALL 为 `<Documents>/SkyrimModTranslationWorkspaces`。命名 MUST 依次执行未截断安全候选派生、带 `source_kind` 的强类型 session/import Mod 名收敛和工作区占用冲突处理；session MUST 持久化 finalized `.value`，导入目标 MUST 直接使用 finalized `.import_name`，不得自行拼接归档扩展名。摘要后缀是否由截断阶段添加 MUST 使用结构化证据而非字符串后缀启发式。session、导入目标与工作区名称 MUST 使用现有 `safe_file_name()` 并限制为 80 个 UTF-16 code unit；归档收敛 MUST 同时为摘要和 `.zip/.7z` 扩展名预留空间，工作区选择 MUST 拒绝未收敛输入。

#### Scenario: 80 单元归档 stem
- **WHEN** ZIP 或 7Z 的安全 stem 已达到 80 UTF-16 code unit
- **THEN** finalized 值为摘要和对应归档扩展名共同截断，`.import_name` 包含原扩展名且仍不超过 80，调用者不再自行拼接后缀

#### Scenario: 天然名称与摘要后缀相同
- **WHEN** 未截断 Mod 名天然以当前 `-digest8` 结尾且该名称已被占用
- **THEN** 工作区选择追加新的 `-digest8`，仅在该候选也冲突时追加 `-2`，不得把天然后缀误判为截断 provenance

#### Scenario: 首次同名输入
- **WHEN** 默认根目录尚无占用且输入 Mod 名合法
- **THEN** 系统使用安全化 Mod 名作为工作区名

#### Scenario: 同名内容变化
- **WHEN** 同名输入的 digest 与已有 session 不同
- **THEN** 系统创建 `<Mod名>-<digest前8位>` 工作区，不覆盖旧工作区

#### Scenario: 名称仍冲突
- **WHEN** 基础名和 digest 后缀名均被不相关目录占用
- **THEN** 系统追加 `-2`、`-3` 直到获得未占用候选

### Requirement: 不可变 session 与非权威缓存
`.workflow/smt-session.json` MUST 以不替换既有目标的原子操作首次创建，正常运行只能验证，MUST NOT 静默重写 workspace_id、Mod、游戏、指纹或导入路径。`cli-state.json` SHALL 只是可丢弃缓存；复用工作区前 MUST 验证 marker、session、导入路径、目标摘要、事务状态和额外输入。

#### Scenario: 相同身份再次运行
- **WHEN** cache 指向一个完全通过验证且身份相同的工作区
- **THEN** CLI 复用既有 session、mod_name 和导入路径，不重新导入或覆盖 session

#### Scenario: cache 指向不存在目录
- **WHEN** 运行命令发现映射目标不存在
- **THEN** 它在全局锁下移除失效映射但不修改其他工作区，并扫描默认根目录直属子目录

#### Scenario: 扫描找到多个匹配 session
- **WHEN** cache 不能裁决且直属扫描找到多个合法相同身份工作区
- **THEN** CLI 返回退出码 `6`，列出全部候选，不按修改时间或排序静默选择

### Requirement: 无死锁 reservation 和进程文件锁
CLI SHALL 使用基于 `LockFileEx` 的 `SmtProcessFileLock`。全局锁只能短时持有，持有全局锁时 MUST NOT 等待 reservation 或工作区锁。新工作区 SHALL 使用 reservation 专用锁，session 工作区 SHALL 使用 `.workflow/smt-operation.lock`；独占持有者可写诊断元数据，共享持有者 MUST NOT 写锁文件。

#### Scenario: 两个进程同时导入相同输入
- **WHEN** 第一个进程已登记并持有相同身份 reservation，第二个进程发现该 reservation
- **THEN** 第二个进程释放全局锁后才等待 reservation 锁，超时返回 `6`，不得并行创建第二个工作区

#### Scenario: 不同输入并行初始化
- **WHEN** 两个进程为不同复合身份创建不同工作区
- **THEN** 全局锁只串行化名称/reservation 提交，耗时初始化和工具准备可在各自 reservation 锁下并行

#### Scenario: 共享状态读取
- **WHEN** `status` 或 `output` 在 `run/resume` 持有独占工作区锁时查询
- **THEN** 查询短时重试后返回 `busy=true` 与退出码 `1`，不得读取半写状态或覆盖锁元数据

### Requirement: 事务导入与崩溃恢复
输入 MUST 先进入 `mod/.smt-import-<uuid>.partial`，只有目标摘要和源 manifest 均验证后才能原子改名为正式路径。session 与 fingerprint mapping MUST 在 committed 之后写入。失败 MUST 删除本次 staging，但不得删除已初始化工作区或用户数据。

#### Scenario: 导入复制中断
- **WHEN** 复制、目标验证或源二次验证失败
- **THEN** 正式 `mod/` 目标、session 和 mapping 均不存在；CLI 仅在其拥有且已初始化的 reservation 工作区写原子失败报告

#### Scenario: session 已写但 mapping 未提交时崩溃
- **WHEN** 后续运行取得无人持有的 reservation 锁，并验证工作区 session 与导入目标合法
- **THEN** 它在全局锁下补写 mapping、删除 reservation 记录并复用工作区

#### Scenario: reservation 没有合法 session
- **WHEN** 崩溃后 reservation 工作区没有合法 session
- **THEN** CLI 保留原目录和诊断记录，为新尝试分配其他名称，doctor 只报告不清理

### Requirement: 单输入单 session
每个 CLI 工作区 SHALL 只绑定一个输入指纹和一个当前 Mod。所有 queue 调用 MUST 同时传入 session 的 `--mod-name`、`--source-path` 与 `--limit 1`。额外未登记输入 MUST NOT 被自动吸收到 session。

#### Scenario: 用户手动加入第二个 Mod
- **WHEN** 同一工作区 `mod/` 出现不属于 session 的额外输入
- **THEN** doctor 报告未登记输入，CLI 继续精确过滤当前 Mod；若额外输入影响项目状态，则投影 `needs_user_input`

### Requirement: 工具准备遵循用户选择
新工作区 MUST 将 `--tool-setup auto|manual|skip` 原样传给 `init_workspace.py`，默认 `auto`，且初始化后 MUST NOT 重复相同安装。复用工作区的 auto SHALL 幂等验证并仅准备缺失/损坏/版本不匹配工具，manual 只检查，skip 完全跳过。

#### Scenario: CI 效果回归
- **WHEN** 效果测试执行 `run fixture.zip --game skyrim-se --tool-setup skip`
- **THEN** 流程不得下载 Python、.NET SDK 或外部工具，并可使用 fake runner 验证编排合同
