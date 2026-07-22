# SMT 唯一用户入口设计

## 1. 目标

项目目前要求用户和 Agent 分别调用工作区初始化、工具准备、输入扫描、队列、恢复、状态查询和产物定位脚本。底层能力已经存在，但公开入口分散，导致用户学习成本高，也增加了 Agent 选错脚本、跳过状态刷新或错误解释内部退出码的风险。

本变更新增一个公开入口：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se
```

第一版提供五个子命令：

```powershell
python scripts\smt.py run <Mod路径> --game skyrim-se
python scripts\smt.py status
python scripts\smt.py resume
python scripts\smt.py doctor
python scripts\smt.py output
```

`smt.py` 是普通用户和顶层 Agent 的唯一公开控制入口。现有初始化、准备、队列、状态机、恢复和 QA 脚本继续作为内部实现，不因本变更移动、重命名或重构。

## 2. 范围

第一版负责：

- 根据 Mod 输入生成稳定指纹；
- 在 Windows 文档目录下选择、创建或复用一个单 Mod 工作区；
- 把用户明确指定的 Mod 原样复制到工作区 `mod/`；
- 按用户选择准备或检查工具；
- 复用现有状态机，精确推进当前 Mod 的低风险非 GUI 任务；
- 把现有 workflow state/tasks 投影成稳定的公开结果；
- 显示进度卡、下一步和标准产物路径；
- 为 Agent 提供结构固定的 JSON 结果。

第一版不负责：

- 引入 pip/PATH 安装式 `smt` 命令；
- 改变 `[tool.uv] package = false`；
- 重构现有 workflow、queue、QA 或 adapter；
- 在 CLI 内调用语言模型生成译文；
- 扩展 LexTranslator 或 xTranslator GUI 自动化能力；
- 让一个 CLI session 管理多个 Mod；
- 让 `doctor` 自动安装、修复或清理；
- 自动安装到游戏目录、MO2 或 Vortex；
- 新增或修改 workflow state 枚举。

## 3. 总体架构

新增两个公开模块：

- `scripts/smt.py`：定义命令行参数，调用内部门面，并成为唯一可以向 stdout/stderr 输出的模块。
- `scripts/smt_cli.py`：组合路径解析、指纹、工作区缓存、锁、事务导入、子进程监管、任务推进和结果投影。该模块不调用 `print`，只返回结构化结果。

为避免把 Win32 API、指纹协议和五个命令全部塞入一个超大脚本，允许新增不带 CLI 的私有帮助模块：

- `scripts/smt_windows.py`：延迟加载 Known Folder、`LockFileEx` 和 Job Object；
- `scripts/smt_fingerprint.py`：定义不可变输入 manifest、目录二进制合同和归档/目录指纹。

`smt_cli.py` 仍是唯一内部门面，私有模块不解析命令行、不向用户输出，也不成为 Agent 或 workflow task 的入口。

内部门面统一返回不可变结果对象。实现可以为嵌套字典定义更具体的 TypedDict，但以下顶层字段和空值语义必须稳定：

```python
class ArtifactInfo(TypedDict):
    path: str
    exists: bool
    kind: str
    validated: bool | None
    validation_evidence: str | None


@dataclass
class CliResult:
    command: str
    exit_code: int
    outcome: str | None
    message: str
    workspace: str | None
    mod_name: str | None
    game_id: str | None
    workflow_state: str | None
    state_snapshot: bool
    state_generated_at: str | None
    state_generated_at_timezone: str | None
    refreshed_by_this_command: bool
    busy: bool
    next_action: dict[str, object] | None
    progress_card_path: str | None
    progress_card: str
    output_paths: dict[str, ArtifactInfo]
    details: list[str]
    diagnostics: list[str]
    diagnostic_log_path: str | None
    underlying_exit_codes: list[int]
```

调用关系保持为：

```text
smt.py
  -> smt_cli.py
  -> init_workspace / queue prepare / resume_workflow / canonical refresh
  -> 现有 readiness / state / tasks / QA
  -> CLI 只做结果投影和用户输出
```

底层命令始终以独立 Python 子进程运行。`smt_cli.py` 统一设置工作目录、UTF-8 环境、超时、进程树监管和以下环境变量：

```text
SKYRIM_CHS_WORKSPACE_ROOT
SKYRIM_CHS_PLUGIN_ROOT
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
```

`SKYRIM_CHS_PLUGIN_ROOT` 必须根据 `smt_cli.py` 的真实位置生成。调用者环境中已有的同名值不可信，也不能覆盖解析结果。

## 4. 公开命令合同

### 4.1 `run`

```text
run <input> --game <id>
            [--workspace <path>]
            [--workspace-root <path>]
            [--tool-setup auto|manual|skip]
            [--timeout-seconds <N>]
```

第一版接受：

- 普通目录；
- `.zip`；
- `.7z`。

第一版拒绝：

- `.rar`；
- 单独作为顶层输入的 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`；
- symlink、junction、reparse point、非普通文件和多硬链接文件；
- 真实游戏目录、MO2/Vortex 目录或现有路径安全策略禁止的位置。

非交互新建工作区时必须传 `--game`。交互终端可以选择并确认已安装的 Game Profile。已有工作区以 marker 中的 `game_id` 为准；显式参数与 marker 冲突时返回工作区冲突。

`run` 的顺序固定为：

```text
验证输入
-> 计算指纹
-> 解析或保留工作区
-> 新工作区初始化并在初始化内部执行所选 tool-setup；复用工作区执行对应的幂等检查
-> 新工作区事务导入；复用工作区重新验证既有导入
-> queue prepare（精确过滤当前 session）
-> canonical refresh
-> 状态驱动推进
-> 输出公开结果、进度卡和产物路径
```

`run` 不无条件调用完整 `run_translation_queue.py --mode workflow`。只有现有状态机把相关完整流程入口生成为当前 Mod 的获授权低风险任务时，CLI 才能精确执行该任务。

### 4.2 `status`

`status` 定位工作区后，以短时共享锁读取最近一次生成的 marker、session、workflow state、workflow tasks 和 progress card。它不刷新扫描、状态或 QA，也不生成替代进度卡。

成功读取时退出码始终为 `0`，即使快照本身为 `blocked` 或 `qa_failed`。JSON 必须包含：

```json
{
  "state_snapshot": true,
  "state_generated_at": "<最近状态生成时间>",
  "state_generated_at_timezone": null,
  "refreshed_by_this_command": false
}
```

progress card 缺失或不可读时返回 `1`。工作区或 session 身份无效时返回 `6`。共享锁在短时重试后仍无法获取时返回 `1`，并令 `busy=true`；不得读取可能处于半写状态的文件。

### 4.3 `resume`

`resume` 验证当前 session 后获取工作区独占锁，运行与 `run` 相同的状态驱动推进循环。CLI 从 `qa/workflow_tasks.json` 精确选择当前 session Mod 的任务，然后调用：

```powershell
python scripts\resume_workflow.py `
  --mode safe `
  --mod-name "<session.mod_name>" `
  --task-id "<selected_task_id>" `
  --include-serial `
  --timeout-seconds <N>
```

内部 `resume_workflow.py` 返回“没有可执行任务”的退出码 `2` 时，公开命令把它记录为底层诊断。若刷新后没有 Agent、GUI、人工或阻断任务，公开结果为成功 no-op，退出码 `0`；若存在这类安全暂停条件，则按对应公开结果返回 `3`。

### 4.4 `doctor`

`doctor` 是诊断命令，不是修复命令。

允许：

- 读取平台、Python、插件根和必要脚本信息；
- 通过只读模式或纯检测函数检查 decoder、adapter 和 GUI 工具配置；
- 检查全局缓存、reservation、marker、session 和 import 完整性；
- 扫描默认工作区根目录的直属子目录；
- 报告未登记工作区、partial import、失效映射和额外 Mod 输入；
- 写入 CLI 自己的诊断日志或隔离临时目录。

禁止：

- 安装工具；
- 重建 adapter；
- 清理 partial；
- 删除失效映射；
- 修改 `tools.local.json`；
- 刷新 readiness/state/tasks；
- 修改 session；
- 自动认领未登记工作区。

如果现有检测脚本默认会改写 QA 或状态报告，`doctor` 必须使用其只读/check 模式、把输出重定向到 CLI 临时目录，或直接调用纯检测函数。

### 4.5 `output`

`output` 使用 session 的精确 `mod_name` 和现有路径合同显示：

```text
out/<ModName>/汉化产出/final_mod
out/<ModName>/汉化产出/intermediate
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

同时显示严格 QA、人工测试计划、人工测试验证和 provenance 证据路径，并分别报告：

```text
可以进入人工游戏测试：是/否
人工游戏测试已验证：是/否
```

不得使用含义模糊的“允许交付”。产物尚不存在不表示命令失败，退出码仍为 `0`。

`--open` 只接受预定义目标：

```text
root
final-mod
intermediate
package-directory
```

CLI 在共享锁内完成路径验证，释放锁后调用系统打开。目标不存在、越出工作区或不是预定义目标时返回 `1`。CLI 绝不复制或安装到游戏、MO2 或 Vortex。

## 5. 工作区寻址

运行时通过 Windows Known Folder API 获取 Documents 和 Local AppData。禁止通过 `Path.home() / "Documents"` 或仅信任 `%LOCALAPPDATA%` 猜测路径。Known Folder 获取失败时返回环境不可用 `5`，不得静默回退。

Windows API 必须在函数调用时延迟加载。模块导入、`compileall` 和 `python scripts/smt.py --help` 在非 Windows 环境必须成功；执行真实命令时才返回当前仅支持 Windows。

默认工作区根目录为：

```text
<Documents>\SkyrimModTranslationWorkspaces
```

全局 CLI 状态目录为：

```text
<LocalAppData>\SkyrimModTranslation
```

`run` 的工作区解析优先级为：

```text
显式 --workspace
-> 当前目录或父目录中的 workspace，但仅在 session 与本次输入复合身份匹配时使用
-> run 输入的复合指纹映射
-> Known Folder 默认工作区根目录中分配新工作区
```

如果 `run` 只是碰巧从另一个合法工作区中启动，且没有显式传 `--workspace`，CLI 忽略不匹配的当前目录工作区，继续查询输入映射或创建新工作区。只有用户显式指定了不匹配工作区时才返回 `6`。

`status`、`resume`、`doctor` 和 `output` 的解析优先级为：

```text
显式 --workspace
-> 当前目录或父目录中的合法 workspace marker
-> 最近活动工作区
```

如果 `doctor` 没有定位到选定或最近工作区，它仍可以只读检查默认工作区根目录的直属子目录；`status`、`resume` 和 `output` 则明确报告没有可用工作区。

`--workspace-root` 只覆盖 `run` 分配新工作区时的默认根目录。

显式 `--workspace` 的规则：

- 路径不存在：允许在该路径初始化；
- 路径为空：允许初始化；
- 合法且 session 身份匹配：允许复用；
- 非空但不是工作区：返回 `6`；
- marker、session 或输入身份不匹配：返回 `6`；
- 绝不清空或覆盖不匹配目录。

## 6. 输入身份与目录指纹

工作区映射键是复合身份，不是单独 SHA-256：

```text
smt-input-v1:<game_id>:<source_kind>:<digest>
```

`source_kind` 第一版只允许 `zip`、`7z` 和 `directory`。复合身份、session 的 `fingerprint_algorithm` 和实现分支统一属于 `smt-input-v1`。目录分支使用 `SMT-INPUT-DIR\0` 加 version 1 的二进制合同；ZIP 和 7Z 分支使用归档文件 SHA-256。相同归档使用不同 Game Profile 时不得复用，同一摘要来自不同来源类型时不得复用。算法升级时必须使用新的算法标识，不能把新旧摘要混用。

首次创建 session 时，`derive_mod_name_candidate()` 从目录名或归档文件去除受支持扩展名后的 stem 派生未截断 `safe_file_name()` 候选，`finalize_mod_name(candidate, digest, source_kind=...)` 再为摘要与归档扩展名共同预留预算，收敛出不可变 `FinalizedModName(source_kind, value, import_name, digest_suffix_applied, digest_prefix)`。session 持久化普通字符串 `.value`，导入目标直接使用 `.import_name`，不得自行拼扩展名；两者均必须 safe 且不超过 80 UTF-16 code unit。`choose_workspace_name()` 只接收该结构并处理占用冲突，不得根据 `.value` 是否以 `-digest8` 结尾猜测摘要来源。同一复合身份从其他路径或改名副本再次运行时复用既有 session 的 `mod_name` 和导入路径，不重新命名或重复导入。

归档以 1 MiB 分块流式计算文件 SHA-256。计算前后比较 `(st_dev, st_ino, st_size, st_mtime_ns)`；输入在计算期间变化时停止。归档本身也必须通过普通文件、链接/reparse 和多硬链接检查。目标复制和目标 SHA-256 验证完成后，再检查一次源归档身份元组；任何变化都使导入失败。

目录指纹函数同时返回不可变 manifest：

```python
@dataclass(frozen=True)
class InputEntry:
    relative_path: str
    entry_type: Literal["directory", "file"]
    size: int
    sha256: str | None
    identity: FileIdentity | None


@dataclass(frozen=True)
class InputManifest:
    source_kind: Literal["directory", "zip", "7z"]
    entries: tuple[InputEntry, ...]
    digest: str
    source_identity: FileIdentity | None
```

`FileIdentity` 的字段固定为 `(device, inode, size, mtime_ns)`。归档及目录根身份记录在 `source_identity`；每个目录 entry 必须保留 no-follow `identity`，其 `sha256` 为 `None`。identity 只用于不可变绑定和变化检测，不进入目录 digest。

目录使用 `smt-input-v1` 二进制合同：

```text
ASCII magic: SMT-INPUT-DIR\0
uint16 big-endian version: 1
uint64 big-endian entry count
重复 entry：
  uint8 type（1=目录，2=文件）
  uint32 big-endian UTF-8 路径字节长度
  NFC 规范化的 POSIX 相对路径 UTF-8 字节
  若为文件：
    uint64 big-endian 文件大小
    32 字节原始 SHA-256
```

具体规则：

- 使用 `discover_regular_tree()` 拒绝 symlink、junction、reparse point、非普通文件和多硬链接文件；
- 在共享 discovery 前绑定根目录和每个目录的 no-follow identity，在每次目录遍历前、`scandir` 后、共享 discovery 后及 manifest 返回前重新验证；发现后被替换为 symlink/junction/reparse 或其他 identity 时立即拒绝；
- 路径分隔符统一为 `/`；
- 路径使用 Unicode NFC；
- 不把根目录自身作为 entry；
- 包含空目录；
- 按规范化路径的 UTF-8 字节升序排列；
- 使用规范化路径的 Windows case-insensitive key 检测冲突，例如 `Data/A.txt` 与 `data/a.txt` 同时存在时拒绝；
- 不包含时间戳、权限等与 Mod 内容无关的元数据；
- 每个文件哈希前后都检查文件身份；
- 复制完成后重新发现并计算导入目标指纹，必须与源指纹一致；
- 目标验证完成后必须重新构建源目录完整 manifest，包括再次计算全部文件 SHA-256；最终 digest、路径、类型、大小和文件身份必须与初始 manifest 一致，用于发现内容覆写、新增、删除、重命名和文件类型变化；
- 仅比较 `(st_dev, st_ino, st_size, st_mtime_ns)` 不足以证明内容未变化，因为同长度覆写可以恢复 mtime；任何最终 manifest 或 digest 变化都使导入失败。

命名分三段：`derive_mod_name_candidate()` 只产生未截断安全候选；`finalize_mod_name()` 根据 `source_kind` 生成强类型 session `.value` 与导入 `.import_name`，两者都限制为 80 个 UTF-16 code unit，归档截断同时为 `-<SHA256前8位>` 和 `.zip/.7z` 预留空间，并以结构化字段记录摘要是否由本阶段添加；`choose_workspace_name()` 只接受 finalized 结构并处理占用冲突，拒绝未收敛输入，不使用字符串后缀启发式。

命名规则：

- 首次合法名称使用 `<安全化Mod名>`；
- 同名不同内容使用 `<安全化Mod名>-<SHA256前8位>`；
- 名称仍被不相关目录占用时追加 `-2`、`-3`；
- 相同复合身份且已有工作区通过全部验证时直接复用。

## 7. Session 与全局缓存

工作区成功导入后原子写入 `.workflow/smt-session.json`：

```json
{
  "schema_version": 1,
  "workspace_id": "<uuid>",
  "mod_name": "ExampleMod",
  "game_id": "skyrim-se",
  "source_kind": "zip",
  "source_display_name": "ExampleMod.zip",
  "fingerprint_algorithm": "smt-input-v1",
  "source_sha256": "<64位小写十六进制>",
  "import_relative_path": "mod/ExampleMod.zip",
  "imported_sha256": "<64位小写十六进制>",
  "created_at": "<UTC ISO-8601>"
}
```

session 不保存原始绝对源路径。

对于目录输入，`source_sha256` 和 `imported_sha256` 都表示前述 `smt-input-v1` 目录摘要；对于归档输入，两者表示归档文件的 SHA-256。

session 是不可变身份记录。普通 `run` 只允许首次创建；文件已经存在时只能验证，不能覆盖或修改 `workspace_id`、`mod_name`、`game_id`、指纹身份或 `import_relative_path`。未来 schema 升级必须使用显式迁移流程，不能在普通运行中静默重写。首次提交使用临时文件加“不替换既有目标”的原子重命名；发现目标已存在时转入身份验证。

`<LocalAppData>\SkyrimModTranslation\cli-state.json` 是可丢弃缓存，包含 schema 版本、最近活动工作区、复合身份映射和临时 reservation。它不是 workflow 或输入身份的权威来源。

全局缓存、reservation 更新和导入失败报告必须先在同一文件系统内写入临时文件，刷新内容后再用 `os.replace()` 原子提交。session 使用上一段定义的原子但不可替换提交。原子提交用于防止内容撕裂；跨进程互斥仍由 `SmtProcessFileLock` 保证。

复用前必须验证：

1. 工作区存在且是普通目录；
2. 工作区不属于插件源码仓库；
3. marker 存在、schema 合法且游戏匹配；
4. session 存在且 schema 可识别；
5. session 的游戏、来源类型、算法版本和 SHA-256 匹配；
6. `import_relative_path` 位于 `mod/` 下；
7. 导入目标存在，且不是链接、junction 或 reparse point；
8. 导入目标指纹匹配；
9. 工作区没有未完成导入事务；
10. `mod/` 中没有会影响当前 session 的未登记额外输入。

运行命令发现失效映射时可以从缓存移除该映射，但不能删除或修改原工作区。随后只扫描默认工作区根目录的直属子目录寻找匹配 session。找不到时才新建工作区。`doctor` 只报告失效映射，不修改缓存。

直属扫描结果的选择规则固定为：

- 恰好一个合法匹配：复用；
- 多个合法匹配且有效缓存明确指向其中一个：使用缓存目标；
- 多个合法匹配且缓存不能裁决：返回工作区冲突 `6`，在 diagnostics 列出全部候选；
- 不按修改时间、目录名或扫描顺序静默选择。

如果用户手动加入第二个无关 Mod，CLI 仍必须给 queue 传入：

```text
--mod-name <session.mod_name>
--source-path <session.import_relative_path>
--limit 1
```

CLI 不处理“发现的第一个输入”。`doctor` 报告未登记输入；如果额外输入已经影响 readiness 或 workflow state，公开结果为 `needs_user_input`，要求移走输入或为其建立新工作区。

## 8. Reservation、锁和事务导入

### 8.1 锁实现

新增 `SmtProcessFileLock`，在 Windows 上延迟调用 `LockFileEx`。真正的锁所有权属于打开的文件句柄，不由锁文件是否存在决定。

- 独占模式：全局缓存更新、reservation 更新、`run`、`resume`；
- 共享模式：`status`、`output`；
- `run/resume` 的独占持有者可以在成功获取后写 PID、命令和开始时间，信息只用于诊断；
- `status/output` 的共享持有者不写锁文件，也不覆盖独占持有者的诊断元数据；
- 不根据 PID 猜测 stale；
- 不自动删除锁文件；
- 竞争超时按命令合同返回 `1` 或 `6`。

新实现不能复用现有基于 `O_CREAT | O_EXCL` 的 `WorkflowLock` 或 `ResourceLock`。

### 8.2 两阶段 reservation

新工作区尚不存在时，不能提前创建 `.workflow/smt-operation.lock`，否则会破坏 `init_workspace.py` 的空目录前置条件。因此初始化期使用 Local AppData 下的 reservation 专用锁：

```text
<LocalAppData>\SkyrimModTranslation\reservation-locks\<workspace-id>.lock
```

锁顺序的总规则是：全局锁只能短时持有；持有全局锁时，绝不等待 reservation 锁或工作区锁。流程为：

```text
获取全局 cli-state 独占锁
-> 校验缓存并分配名称
-> 在 cli-state.json 写入 reservation
-> reservation 所有者对其新建且唯一的 reservation 锁做一次非阻塞获取；失败则中止
-> 释放全局锁
-> 初始化工作区、准备工具并事务导入
-> 初始化创建 .workflow 后获取工作区 smt-operation 独占锁
-> 写 session
-> 重新获取全局锁并提交 fingerprint 映射
-> 删除已提交 reservation
-> 释放全局锁
-> 释放 reservation 锁，继续持有工作区锁推进 workflow
```

其他进程在全局锁下发现同一输入已有 reservation 时，只复制 reservation 信息并立即释放全局锁，然后才等待对应 reservation 锁。等待结束后重新验证 mapping/session；竞争超时返回 `6`。提交者可以在持有 reservation 和工作区锁时短时获取全局锁，因为所有反向路径都禁止在持有全局锁时等待下层锁。

reservation 至少记录：

```json
{
  "workspace_id": "<uuid>",
  "path": "<候选工作区绝对路径>",
  "fingerprint_identity": "<复合身份>",
  "pid": 1234,
  "created_at": "<UTC ISO-8601>"
}
```

进程异常退出后 reservation 记录可以保留用于诊断。后续命令在全局锁下读到该记录后必须先释放全局锁，再独占获取对应 reservation 锁；取得所有权后才检查 reservation 工作区：

- session 和导入目标均合法：在全局锁下补写 fingerprint mapping、删除 reservation 记录并复用该工作区；
- 没有合法 session：不删除或复用原路径，把记录视为未完成 reservation，并为新尝试分配其他名称。

`doctor` 只报告，不执行上述恢复提交。

如果相同复合身份已有一个仍持锁的 reservation，新 `run` 在预设短时等待后返回 `6`，并在诊断中指出工作区正在初始化。它不能为同一输入并行创建第二个工作区。无人持锁但未提交的 reservation 按上一段的未完成规则处理。

已建立 session 的工作区使用：

```text
<workspace>\.workflow\smt-operation.lock
```

不同工作区可以并行。同一工作区只能有一个 `run` 或 `resume`。`status` 和 `output` 只能在共享锁成功后读取一致快照；`output --open` 在共享锁内验证路径，释放后再打开。

`doctor` 检查选定工作区或默认根目录直属工作区时，也逐个尝试短时共享锁。锁忙只作为 `busy` 诊断记录；`doctor` 不绕过锁读取正在更新的状态，也不因单个工作区繁忙而修改或清理任何内容。

### 8.3 导入事务

输入先复制到：

```text
mod/.smt-import-<uuid>.partial
```

验证目标指纹后，以原子改名提交到正式路径。状态依次为：

```text
staging -> verified -> committed
```

只有 committed 后才写 session 和全局映射。

失败时：

- 删除本次 staging；
- 不写 session；
- 不写输入映射；
- 不删除已初始化工作区；
- 仅当 CLI 已创建并拥有该 reservation 工作区，且初始化已经创建 `.workflow/` 时，原子写入 `.workflow/smt-import-failure.json`；
- 初始化在工作区创建前失败时只写 CLI 日志，不创建用户指定目录。

## 9. 工具准备

`--tool-setup` 默认值为 `auto`。

新工作区调用：

```powershell
python scripts\init_workspace.py <workspace> `
  --game <id> `
  --tool-setup <auto|manual|skip>
```

初始化脚本已经负责工具准备和初始状态刷新，因此 CLI 不在初始化后无条件重复相同步骤。

复用工作区时：

- `auto`：执行幂等工具验证，只在缺失、损坏或版本不匹配时准备；
- `manual`：只运行检测并显示配置建议；
- `skip`：不调用工具准备，后续状态机可以正常产生工具缺失阻断。

可选 GUI 工具缺失只在当前资源确实需要 GUI 时成为 `needs_gui` 或工具阻断，不在普通非 GUI Mod 上提前失败。

## 10. 状态驱动推进

CLI outcome 是现有 workflow state/tasks 的只读投影，不是新的工作流状态。

```python
def classify_outcome(...) -> PublicOutcome | None:
    ...
```

返回 `None` 表示当前仍有合法低风险任务，应继续推进。返回公开 outcome 表示已经达到稳定结果，应停止。

判断顺序固定为：

1. 当前 session Mod 与 project 都是 `manual_tested`，blocking checks 为空：`completed`；
2. 当前 session Mod state 是 `ready_for_manual_test`，project state 属于 `ready_for_manual_test` 或 `manual_tested`，当前 Mod blocking checks 为空，且不存在影响整个工作区的 global/project blocker：`ready_for_manual_test`；
3. 存在明确不可继续的安全停止条件，例如策略 stop condition 已满足、当前任务已失败且不允许自动重试、能力被 Profile 禁止，或下一动作被标为高风险：按证据投影 `needs_user_input` 或 `blocked`；
4. 存在当前 Mod 的合法低风险非 GUI 任务：返回 `None`；
5. 下一任务要求 `gui:desktop`、持有 `gui:desktop` 锁或 handoff 到 Codex：`needs_gui`；
6. 等待译文、语义校对、模型审阅或翻译决策：`needs_agent_translation`；
7. 等待文件、游戏身份、术语或其他明确用户选择：`needs_user_input`；
8. `qa_failed`、不支持能力、高风险操作、失败任务或无合法推进动作：`blocked`。

`completed` 只等价于最近一次 canonical refresh 后的有效 `manual_tested`。CLI 运行完成、没有自动任务或静态 QA 通过都不能投影成 `completed`。

执行循环为：

```text
按 workflow_refresh.CORE_REFRESH_STEPS 刷新
-> 读取 session/state/tasks/policy
-> 精确选择当前 Mod 的合法低风险任务
-> classify_outcome
-> outcome 不为空时停止
-> 检查本次命令是否已尝试 task_id + evidence
-> 记录刷新前摘要
-> resume_workflow.py --mod-name ... --task-id ...
-> 读取其已刷新的状态
-> 比较刷新后摘要
-> 重复
```

outcome 分类读取 marker/session、workflow state、workflow tasks 和 progress card。编排控制还可以只读 `config/workflow_policy.json`，以取得 `max_same_blocker_attempts=2` 等权威策略。单次命令以 `blocker + evidence` 精确计数，最多尝试两次。跨命令不能从通用 `retry_count` 推导某个 blocker 的次数；仅当 `last_attempt` 的 command/evidence 与当前任务相同、上次状态为 failed/blocked、当前 blocker 与状态摘要均未变化时，直接停止而不再次自动执行。CLI 不建立第二套持久化重试状态。

状态摘要至少包括：

- project state；
- 当前 Mod state；
- blocking checks；
- pending/running/failed task ID；
- next-action 类型；
- evidence 标识。

停止条件：

- 达到公开稳定结果；
- 不存在合法低风险任务；
- 执行前后状态摘要没有变化；
- 同一 `task_id + evidence` 已在本次命令执行；
- 同一 blocker 达到策略上限；
- 达到固定最大步数；
- 达到超时；
- 用户中断。

无进展投影为 `blocked`，退出码 `3`，并在结果中给出最后任务和 evidence。

## 11. 子进程监管和日志

底层命令使用 `Popen` 增量读取输出。完整输出追加到：

```text
<workspace>\.workflow\smt-cli.log
```

在工作区尚未创建时写入：

```text
<LocalAppData>\SkyrimModTranslation\logs\<workspace-id>.log
```

内存只保留最后 200 行。文本界面只显示摘要、阻断原因、progress card 和产物路径；JSON 界面把警告放入 `diagnostics`。

Windows 子进程监管使用：

```text
CREATE_NEW_PROCESS_GROUP
+ Windows Job Object
+ JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
```

CLI 必须以 `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP` 创建底层进程，在主线程恢复前完成 Job Object 创建、`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 配置和 `AssignProcessToJobObject`，然后才调用 `ResumeThread`。这样子进程不能在父进程加入 Job 前创建逃逸后代。超时时终止 Job 并返回 `124`。Ctrl+C 时先发送 `CTRL_BREAK_EVENT`，短暂等待后关闭 Job，保证后代进程退出并返回 `130`。如果进程无法加入或恢复 Job Object，CLI 必须立即终止已启动进程，并通过明确的 `taskkill /PID <pid> /T /F` 兜底；无法建立可靠监管时返回环境不可用 `5`，不能继续无人监管的工作流。

## 12. 公开结果与退出码

公开结果与退出码是两个维度。Agent 必须读取 JSON 的 `outcome`，不能把所有非零码解释为底层失败。

| 结果或错误 | 退出码 |
| --- | ---: |
| `completed` | 0 |
| `ready_for_manual_test` | 0 |
| 成功 no-op | 0 |
| `needs_agent_translation` | 3 |
| `needs_gui` | 3 |
| `needs_user_input` | 3 |
| 普通安全阻断或 QA 阻断 | 3 |
| 输入格式不支持，或 `blocked` 的直接原因是 Profile/资源能力不支持 | 4 |
| 工具或运行环境不可用 | 5 |
| marker、session 或工作区身份冲突 | 6 |
| 子进程超时 | 124 |
| 用户中断 | 130 |
| 未分类内部或读取失败 | 1 |
| argparse 参数错误 | 2 |

底层脚本原始退出码保留在 `underlying_exit_codes`，不直接成为公开合同。

因此 `outcome` 与退出码不形成一对一关系：例如普通 QA 阻断和能力不支持都可以投影为 `blocked`，但前者返回 `3`，后者返回 `4`；由于必需工具缺失而无法执行的结果返回 `5`，而仅等待获授权 GUI 操作的 `needs_gui` 返回 `3`。

## 13. 文本和 JSON 输出

全局参数：

```text
--format text|json
```

默认 `text`。顶层 Agent 固定使用 `json`。

`text` 模式显示 outcome、简短说明、原始 progress card、下一步和输出路径。CLI 不自行编造阶段完成信息。

`json` 模式下 stdout 只能包含一个 schema v1 JSON 对象。所有字段始终存在；缺失值使用 `null`、空数组或空对象，不因 outcome 改变结构。stderr 只用于 argparse 参数错误或 CLI 在能够构造 JSON 前发生的致命错误。

最小合同为：

```json
{
  "schema_version": 1,
  "command": "run",
  "outcome": "needs_agent_translation",
  "exit_code": 3,
  "message": "需要 Agent 生成并校对译文",
  "workspace": "D:/.../ExampleMod",
  "mod_name": "ExampleMod",
  "game_id": "skyrim-se",
  "workflow_state": "candidates_extracted",
  "state_snapshot": false,
  "state_generated_at": "2026-07-22 13:45:00",
  "state_generated_at_timezone": null,
  "refreshed_by_this_command": true,
  "busy": false,
  "next_action": {
    "kind": "agent_translation",
    "summary": "生成并校对玩家可见文本译文",
    "artifacts": ["work/normalized/ExampleMod/..."]
  },
  "progress_card_path": ".workflow/progress_card.md",
  "progress_card": "<原始 Markdown 内容>",
  "output_paths": {
    "final_mod": {
      "path": "out/ExampleMod/汉化产出/final_mod",
      "exists": false,
      "kind": "directory",
      "validated": null,
      "validation_evidence": null
    }
  },
  "details": [],
  "diagnostics": [],
  "diagnostic_log_path": ".workflow/smt-cli.log",
  "underlying_exit_codes": []
}
```

`state_generated_at` 原样继承权威状态文件的现有时间字符串。当前 writer 使用不带时区的本地时间，因此 CLI 不得把它改写或标注为 UTC，`state_generated_at_timezone` 固定为 `null`；未来只有权威状态文件提供明确时区时才能填入该字段。

第一版不写 `.workflow/smt-command-result.json`，以保持 `status` 和 `output` 的只读语义。

## 14. Agent 使用合同

顶层 Agent 收到“翻译这个 Mod”后：

1. 首次只调用 `python scripts/smt.py --format json run ...`；
2. 根据 JSON outcome、next action 和 artifacts 执行语言翻译或获授权的 GUI 工作；
3. 后续只调用 `python scripts/smt.py --format json resume`；
4. 查询状态和产物只调用 `python scripts/smt.py --format json status` 与 `python scripts/smt.py --format json output`；
5. 不自行组合初始化、queue、状态刷新、恢复和 QA 脚本。

底层脚本继续供开发者诊断和 CLI 内部编排使用。文件类型 Skills 仍可以描述受控 adapter 和具体文本规则，但用户入口、运行期编排和恢复 Skills 必须统一引用公开 CLI。

## 15. 测试策略

### 15.1 纯逻辑测试

新增 `tests/test_smt_cli.py`，覆盖：

- ZIP、7Z 和目录指纹；
- Unicode NFC、Windows 大小写冲突、空目录；
- 链接、junction、reparse point 和多硬链接拒绝；
- 80 UTF-16 code unit 名称限制；
- 复合身份和工作区命名；
- marker/session/import 匹配；
- outcome 优先级和 `None` 继续状态；
- 当前 Mod 精确任务选择；
- 状态摘要与无进展检测；
- outcome、退出码和 JSON schema；
- `smt_cli.py` 不写 stdout/stderr。

### 15.2 工作区测试

新增 `tests/test_smt_cli_workspace.py`，覆盖：

1. 相同 ZIP、相同游戏复用工作区；
2. 相同 ZIP、不同游戏不复用；
3. 同名不同内容使用 SHA 后缀；
4. 同名同 SHA 但目录不属于该输入时追加数字后缀；
5. 映射指向不存在目录时自动失效；
6. session 与 marker 游戏不一致时拒绝复用；
7. 导入中断后不留下正式 `mod/` 半成品；
8. 缓存损坏时不覆盖现有工作区；
9. 显式工作区的初始化、复用和身份冲突；
10. 额外未登记 Mod 输入诊断；
11. `doctor` 不修改缓存、session、工具配置或 workflow state；
12. `output` 只使用当前 session Mod 的公开路径合同。

### 15.3 Windows 并发和进程测试

覆盖：

- 两个进程不会获得同一个 reservation；
- 不同工作区可以并行初始化；
- 同一工作区的两个 `run/resume` 不能并发；
- `status/output` 通过共享锁读取一致快照；
- 共享锁超时返回 `busy=true` 和退出码 `1`；
- 在父进程主线程恢复前已完成 Job 分配，极速创建孙进程也不能逃逸；超时和 Ctrl+C 后没有残留后代进程。

### 15.4 平台与效果回归

Ubuntu 必须通过：

- `python -m compileall scripts`；
- 导入 `smt` 和 `smt_cli`；
- `python scripts/smt.py --help`。

非 Windows 执行真实命令返回 `5`，不能在模块导入阶段崩溃。

效果回归增加一个安全 ZIP fixture：初始化临时工作区、事务导入、queue prepare、canonical refresh，在需要 Agent 翻译处稳定暂停；再次运行相同输入复用工作区；修改输入后创建新工作区。真实工具下载、真实 GUI 和真实游戏测试不进入自动 CI。

效果回归必须显式使用 `--tool-setup skip` 或注入 fake child-process runner，不能触发默认 `auto`：

```powershell
python scripts\smt.py run fixture.zip `
  --game skyrim-se `
  --tool-setup skip
```

不同工作区并行初始化测试使用 stub 初始化/工具脚本，只验证 reservation、路径分配、锁并发和 session 提交，不下载两套 Python、.NET SDK 或外部工具。

当前 `.gitignore` 忽略整个 `tests/`，且工作树中存在不属于本变更的本地测试文件。实现必须把该规则改成精确 allowlist：目录内其他未跟踪文件继续忽略，但 `tests/test_smt_cli.py` 和 `tests/test_smt_cli_workspace.py` 明确解除忽略并正常受 Git 跟踪；不能依赖 `git add -f` 隐式提交新测试，也不能让本变更顺带纳入其他本地测试文件。

## 16. 文档和治理迁移

更新：

- `README.md`：唯一入口和最短示例；
- `USER_GUIDE.md`：五个公开子命令；
- `ADVANCED_USER_GUIDE.md`、`developer_guide.md`、`scripts/README.md`：底层脚本标为内部诊断或开发入口；
- `AGENTS.md`：Agent 首次使用 `run --format json`，后续只使用 `resume/status/output`；
- 用户入口、运行期编排和恢复 Skills：改用公开 CLI 合同；
- 不把 `smt.py` 加入 `config/workflow_policy.json` 的 `allowed_entrypoint_scripts` 或其他 workflow 授权集合；`smt.py` 是状态机外部控制门面，不能成为 `next_actions` 或 `workflow_tasks`；
- CI 静态规则：阻止普通用户和顶层 Agent 文档重新组合底层脚本。

第一版不新增 `public_control_entrypoints` 配置字段。公开入口身份由 CLI 合同测试和文档/Skills 静态检查固定，避免误入 `allowed_scripts()` 和 capability 授权逻辑。未来若需要配置化，必须使用与 workflow policy 授权集合完全隔离的字段和读取路径。

现有五类合并门禁必须全部成功：

- static；
- windows-smoke；
- windows-fallout4-adapters；
- windows-fallout4-workflow；
- effect-regression。

## 17. 合入标准

变更只有满足以下条件才能合入：

- 五个公开子命令的文本和 JSON 合同通过；
- 当前 session Mod 的精确任务选择无法越界到其他输入；
- 事务导入、reservation、共享/独占锁和进程树终止测试通过；
- 相同复合身份可以复用，输入或游戏变化时不会污染旧工作区；
- Ubuntu 静态 CI 不受 Windows API 影响；
- Windows smoke 和 effect regression 通过；
- 普通用户和顶层 Agent 文档不再要求组合底层脚本；
- `ready_for_manual_test` 与 `completed` 保持严格区分；
- `doctor` 保持只诊断；
- 不访问真实游戏目录、真实 MO2/Vortex 目录，也不直接修改受保护二进制。

该变更涉及路径安全、归档输入、reparse point、Windows 锁、Job Object、状态投影和 workflow 边界，属于仓库治理规则中的高风险范围。合入前必须由独立 Agent 在最新提交上复核，记录 reviewed commit；审查修复产生新提交后必须再次复核，并等待全部 required checks 完成后再合并。
