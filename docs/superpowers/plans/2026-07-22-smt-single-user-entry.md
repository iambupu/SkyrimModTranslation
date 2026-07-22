# SMT Single User Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 `python scripts\smt.py` 这一唯一公开控制入口，让普通用户和顶层 Agent 通过五个子命令完成单 Mod 工作区创建、事务导入、状态推进、诊断和产物定位，同时保持现有 workflow state/tasks/QA 为唯一权威。

**Architecture:** `scripts/smt.py` 只负责 argparse 和 text/JSON 渲染，`scripts/smt_cli.py` 是无输出的内部门面，`scripts/smt_windows.py` 隔离延迟加载的 Windows Known Folder、LockFileEx 和 Job Object，`scripts/smt_fingerprint.py` 负责不可变输入 manifest。CLI 只调用现有 Python 入口、复用 `workflow_refresh.CORE_REFRESH_STEPS`，并通过 `resume_workflow.py --mod-name --task-id` 精确推进当前 session。

**Tech Stack:** Python 3.11+、pytest 8、Windows ctypes/Win32 API、现有 workspace marker/state/tasks/progress-card JSON/Markdown 合同、OpenSpec。

## Global Constraints

- 权威设计：`docs/superpowers/specs/2026-07-22-single-user-entry-cli-design.md`。
- OpenSpec change：`openspec/changes/add-smt-single-user-entry/`；实现期间逐项勾选其 `tasks.md`。
- 保持 `pyproject.toml` 的 `[tool.uv] package = false`；不增加安装式 console script。
- 不把 `scripts/smt.py` 加入 workflow policy 的授权入口、`next_actions` 或 workflow tasks。
- 不重构现有 workflow 状态机、queue、QA、adapter 或工具安装实现。
- 非 Windows 必须能 compile/import/help；真实子命令返回环境不可用 `5`。
- 测试只使用临时目录、stub runner 和 `--tool-setup skip`，不得下载或调用真实工具。
- 每个任务执行 Red → Green → Refactor；先确认指定测试因目标行为缺失而失败。
- 每个提交只暂存当前任务文件，不覆盖用户已有无关改动。

---

### Task 1: 建立可跟踪测试与固定结果类型

**Files:**

- Modify: `.gitignore`
- Create: `scripts/smt_cli.py`
- Create: `tests/test_smt_cli.py`

**Interfaces:**

```python
PublicOutcome = Literal[
    "completed",
    "ready_for_manual_test",
    "needs_gui",
    "needs_agent_translation",
    "needs_user_input",
    "blocked",
]

class ArtifactInfo(TypedDict):
    path: str
    exists: bool
    kind: str
    validated: bool | None
    validation_evidence: str | None

class NextAction(TypedDict):
    kind: str
    summary: str
    artifacts: list[str]
```

- [ ] **Step 1: 精确解除两个新测试文件的忽略**

把 `.gitignore` 中的 `tests/` 改为以下完整规则：

```gitignore
tests/
!tests/
tests/*
!tests/test_smt_cli.py
!tests/test_smt_cli_workspace.py
```

运行 `git check-ignore -v tests/test_smt_cli.py tests/test_smt_cli_workspace.py tests/test_project_paths.py`。预期前两个不匹配，第三个仍匹配 `tests/*`。

- [ ] **Step 2: 写失败测试**

在 `tests/test_smt_cli.py` 创建标准 `ROOT/sys.path` 引导，并写：

```python
EXPECTED_KEYS = {
    "schema_version", "command", "outcome", "exit_code", "message",
    "workspace", "mod_name", "game_id", "workflow_state",
    "state_snapshot", "state_generated_at", "state_generated_at_timezone",
    "refreshed_by_this_command", "busy", "next_action",
    "progress_card_path", "progress_card", "output_paths", "details",
    "diagnostics", "diagnostic_log_path", "underlying_exit_codes",
}

def test_empty_result_has_stable_schema_and_no_output(capsys) -> None:
    result = empty_result("status")
    assert set(result.to_payload()) == EXPECTED_KEYS
    assert result.to_payload()["state_generated_at_timezone"] is None
    json.dumps(result.to_payload(), ensure_ascii=False)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

def test_artifact_info_is_structured() -> None:
    result = empty_result("output")
    result.output_paths["final_mod"] = {
        "path": "out/Example/汉化产出/final_mod",
        "exists": False,
        "kind": "directory",
        "validated": None,
        "validation_evidence": None,
    }
    assert result.to_payload()["output_paths"]["final_mod"]["exists"] is False
```

- [ ] **Step 3: 确认失败**

运行 `python -m pytest -q tests/test_smt_cli.py`。预期 collection 因 `smt_cli` 不存在而失败。

- [ ] **Step 4: 实现结果骨架**

在 `scripts/smt_cli.py` 使用 dataclass 定义可逐步聚合诊断的 `CliResult`，字段与 `EXPECTED_KEYS` 一一对应；所有路径字段在进入结果对象前转换为 `str`，不得把 `Path` 交给 JSON renderer；`schema_version=1`，缺值使用 `None`、空列表或空字典；`to_payload()` 使用 `dataclasses.asdict()`。增加非空 Windows 路径 JSON 序列化测试。模块导入和 `empty_result()` 不得 print。

- [ ] **Step 5: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py`、`git diff --check`。预期全部通过。提交：`feat(cli): 定义 SMT 公开结果合同`。

---

### Task 2: 实现 smt-input-v1 不可变 manifest

**Files:**

- Create: `scripts/smt_fingerprint.py`
- Create: `tests/test_smt_cli_workspace.py`
- Reuse: `scripts/file_utils.py:discover_regular_tree`
- Reuse: `scripts/project_paths.py:safe_file_name`

**Interfaces:**

```python
@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int

@dataclass(frozen=True)
class InputEntry:
    relative_path: str
    entry_type: Literal["file", "directory"]
    size: int
    sha256: str | None
    identity: FileIdentity | None

@dataclass(frozen=True)
class InputManifest:
    source_kind: Literal["directory", "zip", "7z"]
    entries: Sequence[InputEntry]  # 实现必须在 __post_init__ 中收敛为不可变 tuple
    digest: str
    source_identity: FileIdentity | None

build_input_manifest(path: Path) -> InputManifest
composite_input_identity(game_id: str, manifest: InputManifest) -> str
verify_source_unchanged(path: Path, manifest: InputManifest) -> None
verify_imported_copy(path: Path, manifest: InputManifest) -> None
derive_mod_name(path: Path) -> str
choose_workspace_name(mod_name: str, digest: str, occupied: Collection[str]) -> str
```

- [ ] **Step 1: 写目录合同失败测试**

```python
def test_directory_manifest_is_stable_and_includes_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "Example"
    (source / "Empty").mkdir(parents=True)
    (source / "Interface").mkdir()
    (source / "Interface" / "menu.txt").write_text("hello", encoding="utf-8")
    first = build_input_manifest(source)
    second = build_input_manifest(source)
    assert first == second
    assert [row.relative_path for row in first.entries] == [
        "Empty", "Interface", "Interface/menu.txt",
    ]
    assert composite_input_identity("skyrim-se", first) == (
        f"smt-input-v1:skyrim-se:directory:{first.digest}"
    )

def test_source_reenumeration_detects_added_file(tmp_path: Path) -> None:
    source = tmp_path / "Example"
    source.mkdir()
    (source / "A.txt").write_text("A", encoding="utf-8")
    manifest = build_input_manifest(source)
    (source / "B.txt").write_text("B", encoding="utf-8")
    with pytest.raises(InputChangedError, match="source input changed"):
        verify_source_unchanged(source, manifest)
```

补充 ZIP/7Z、NFC、空目录、Windows casefold 冲突、symlink/junction/reparse、非普通文件、多硬链接、哈希中变化、复制后新增/删除/重命名/类型变化测试。

- [ ] **Step 2: 写输入和命名测试**

RAR、单独 ESP/ESM/ESL/BSA/BA2 顶层输入必须抛 `UnsupportedInputError`。`choose_workspace_name("Example", "0123456789abcdef", {"Example", "Example-01234567", "Example-01234567-2"})` 必须返回 `Example-01234567-3`；所有名称不超过 80 UTF-16 code units。

- [ ] **Step 3: 确认失败**

运行 `python -m pytest -q tests/test_smt_cli_workspace.py -k "manifest or source or workspace_name or unsupported"`。预期 collection 因模块缺失失败。

- [ ] **Step 4: 实现二进制摘要合同**

固定常量：`DIRECTORY_MAGIC=b"SMT-INPUT-DIR\x00"`、`DIRECTORY_VERSION=1`、`HASH_CHUNK_SIZE=1024*1024`、`FINGERPRINT_ALGORITHM="smt-input-v1"`。entry 编码依次包含类型 byte、UTF-8 路径长度、NFC POSIX 路径、uint64 size、文件 raw SHA-256。按规范化 UTF-8 bytes 排序；casefold key 重复即拒绝。每个文件哈希前后比较完整 `FileIdentity`。

- [ ] **Step 5: 实现双重验证和命名**

目标复制后重新构建完整 manifest 并比较 digest/entries；随后重新计算源目录全部文件 SHA-256 和完整 manifest/digest，除新增/删除/重命名外，还要以回归测试覆盖同长度覆写并恢复 mtime。归档目标验证后重新计算源 SHA-256 并再次比较 identity。名称先用 `safe_file_name()`，再按 UTF-16 code unit 安全截断。

- [ ] **Step 6: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli_workspace.py -k "manifest or fingerprint or source or workspace_name or unsupported or case"` 及 `python -m pytest -q tests/test_project_paths.py`。提交：`feat(cli): 实现不可变 Mod 输入指纹`。

---

### Task 3: 实现 Windows 平台边界

**Files:**

- Create: `scripts/smt_windows.py`
- Modify: `tests/test_smt_cli.py`
- Modify: `tests/test_smt_cli_workspace.py`

**Interfaces:**

```python
documents_directory() -> Path
local_app_data_directory() -> Path

class SmtProcessFileLock:
    __init__(path: Path, mode: Literal["shared", "exclusive"], timeout_seconds: float)
    acquire() -> SmtProcessFileLock
    release() -> None

class ManagedProcess:
    run(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout_seconds: int, log_path: Path) -> ProcessResult
```

- [ ] **Step 1: 写延迟加载测试**

非 Windows 导入 `smt_windows` 必须成功；调用 Known Folder 或真实进程监管才抛 `WindowsEnvironmentUnavailable`。不得在 import 时读取 `ctypes.windll`。默认路径必须来自 Windows Known Folder，不能回退 `Path.home()/Documents` 或环境变量猜测。

- [ ] **Step 2: 写多进程锁测试**

使用 spawn 进程证明：两个共享锁可并行；独占锁阻塞共享/独占；超时抛 `SmtLockTimeout`；进程退出后内核释放锁；锁文件存在不代表锁占用；共享持有者不写/覆盖独占诊断 JSON。

- [ ] **Step 3: 实现 LockFileEx**

函数调用时才 `ctypes.WinDLL`。用 `CreateFileW` 打开稳定锁文件句柄，`LockFileEx` 锁定固定 byte range，`UnlockFileEx` 释放。不实现 PID stale 判断和删除。`run/resume` 独占锁可写 PID/命令/开始时间；共享锁只持句柄。

- [ ] **Step 4: 写并实现进程树监管测试**

fixture 子进程在主线程恢复后立即创建长驻孙进程并写 PID。实现必须以 `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP` 创建父进程，完成 Job 创建/限制配置/分配后才 `ResumeThread`。超时后两个 PID 都必须退出，结果为 `124`，输出尾部最多 200 行；Ctrl+C 先发 CTRL_BREAK，再关闭 Job；Job 分配或线程恢复失败时终止已启动树并以 `taskkill /PID <pid> /T /F` 兜底，公开返回 `5`。

- [ ] **Step 5: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py -k "windows or process"`、`python -m pytest -q tests/test_smt_cli_workspace.py -k "lock or concurrent"`、`python -m compileall -q scripts/smt_windows.py`。提交：`feat(cli): 增加 Windows 锁与进程监管`。

---

### Task 4: 实现缓存、不可变 session、reservation 和事务导入

**Files:**

- Modify: `scripts/smt_cli.py`
- Modify: `tests/test_smt_cli_workspace.py`
- Reuse: `scripts/init_workspace.py`
- Reuse: `scripts/project_paths.py:find_workspace_root`

**Interfaces:**

```python
@dataclass(frozen=True)
class SmtSession:
    schema_version: int
    workspace_id: str
    mod_name: str
    game_id: str
    fingerprint_algorithm: str
    input_identity: str
    source_kind: str
    import_relative_path: str

create_session_no_replace(path: Path, session: SmtSession) -> None
validate_session(workspace: Path, identity: str | None = None) -> SmtSession
resolve_run_workspace(request: RunRequest, manifest: InputManifest) -> WorkspaceResolution
import_input_transactionally(source: Path, resolution: WorkspaceResolution, manifest: InputManifest) -> SmtSession
```

- [ ] **Step 1: 写寻址与不可变 session 测试**

覆盖：显式 workspace 冲突返回 `6`；碰巧位于不匹配 cwd workspace 时 run 忽略它；其他命令按显式、cwd、last active；session 首次 no-replace，第二次只验证且不得改 workspace_id/mod/game/fingerprint/import path；默认根直属扫描找到多个同身份 session 且 cache 不能裁决时返回 `6` 并列出全部候选。

- [ ] **Step 2: 写 reservation 锁序测试**

用 recording lock factory 断言持有 global 时没有 blocking acquire reservation/workspace。相同 reservation 的第二进程必须读取后释放 global 再等待；不同身份的耗时初始化区间可以重叠。session 合法但 mapping 缺失时补登记；无合法 session 的 reservation 保留并为新尝试换名。

- [ ] **Step 3: 写事务失败测试**

在 copier 中途抛异常，断言 `.smt-import-<uuid>.partial` 删除，正式目标/session/mapping 不存在，已初始化工作区保留。只有 CLI 创建并拥有 reservation 且 `.workflow` 已存在时才原子写 `smt-import-failure.json`。

- [ ] **Step 4: 实现 cache/session 协议**

全局状态为 `<LocalAppData>/SkyrimModTranslation/cli-state.json`，含 schema、last workspace、input mappings、reservations。cache 可删且不权威；复用前总是验证 marker/session/import digest/transaction/额外输入。JSON 写入使用同目录临时文件、flush、fsync、replace；session 使用 no-replace 原子创建，普通 run 不迁移。

- [ ] **Step 5: 实现无死锁 reservation 与导入**

固定锁序：短持 global 分配名称/reservation并释放；持 reservation 执行初始化/工具/导入；持 reservation+workspace 时再短持 global 提交 mapping 并删 reservation。输入先复制至 `mod/.smt-import-<uuid>.partial`，目标 digest 和源二次验证都通过才同卷原子 rename；之后写 session，最后写 mapping。

- [ ] **Step 6: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli_workspace.py -k "session or mapping or reservation or transaction or workspace"`、`git diff --check`。提交：`feat(cli): 实现事务工作区与不可变会话`。

---

### Task 5: 实现权威快照、outcome 投影和精确任务推进

**Files:**

- Modify: `scripts/smt_cli.py`
- Modify: `tests/test_smt_cli.py`
- Reuse: `scripts/workflow_refresh.py:CORE_REFRESH_STEPS`
- Reuse: `scripts/workflow_task_policy.py`
- Reuse: `scripts/resume_workflow.py`

**Interfaces:**

```python
refresh_authoritative_state(workspace: Path, runner: CommandRunner, timeout_seconds: int) -> list[int]
select_exact_safe_task(snapshot: WorkflowSnapshot, mod_name: str, now: datetime) -> dict[str, object] | None
classify_outcome(snapshot: WorkflowSnapshot, mod_name: str, selected_task: dict[str, object] | None) -> PublicOutcome | None
state_digest(snapshot: WorkflowSnapshot, mod_name: str) -> str
advance_workflow(workspace: Path, session: SmtSession, services: SmtServices, timeout_seconds: int) -> CliResult
```

- [ ] **Step 1: 写 refresh 与精确任务失败测试**

recording runner 必须观察到 `audit_translation_readiness.py`、`write_workflow_state.py`、`write_workflow_tasks.py`、`write_codex_handoff.py`，顺序来自 `CORE_REFRESH_STEPS`。当 tasks 同时有 OtherMod 和 CurrentMod 时只选择 CurrentMod 的 executable/low/dependencies satisfied/resources available/non-GUI 任务。

- [ ] **Step 2: 写分类优先级参数化测试**

覆盖：project/current 都 manual_tested 且无 blocker → completed；current ready、project ready/manual_tested、无 current/global blocker → ready；存在安全自动任务 → `None`；无自动任务后依次投影 GUI、Agent translation、user input、blocked。当前 Mod ready 但额外 Mod 造成 global/project blocker 时不得 ready。

- [ ] **Step 3: 写精确 resume 和无进展测试**

底层 argv 必须精确等于 `--mode safe --mod-name ExampleMod --task-id task-42 --include-serial --timeout-seconds 60`。不得调用 `run_workflow_tasks.py --limit 1`。执行后 digest 不变返回 `blocked/3`；同一 `(blocker,evidence)` 单次最多两次；跨命令只依赖 last_attempt command/evidence + 上次 failed/blocked + digest/blocker 不变，不使用通用 retry_count。

- [ ] **Step 4: 实现权威读取和分类**

outcome 只读 marker/session、`qa/workflow_state.json`、`qa/workflow_tasks.json`、`.workflow/progress_card.md`；policy 只读重试/步数上限。progress card 原样进入结果。刷新遍历导入的 `CORE_REFRESH_STEPS`，不得维护脚本副本。

- [ ] **Step 5: 实现推进循环**

固定为 refresh → read snapshot → select exact task → classify → execute exact task → read refreshed snapshot → compare digest。停止于稳定 outcome、无合法动作、重复 task+evidence、重复 blocker+evidence、digest 不变、最大步数、超时或中断。内部 exit code 仅进入 diagnostics；resume 内部 `2` 在没有稳定暂停动作时归一为 public no-op `0`。

- [ ] **Step 6: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py -k "refresh or task or outcome or digest or advance or resume"` 和 `python -m pytest -q tests/test_workflow_refresh.py`。提交：`feat(cli): 投影并推进权威工作流状态`。

---

### Task 6: 完成 run、resume 与 tool-setup 编排

**Files:**

- Modify: `scripts/smt_cli.py`
- Modify: `tests/test_smt_cli.py`
- Modify: `tests/test_smt_cli_workspace.py`
- Reuse: `scripts/run_translation_queue.py`
- Reuse: `scripts/setup_workspace_tools.py`

- [ ] **Step 1: 写首次 run 调用链测试**

fake runner 必须按 init → queue prepare → CORE refresh 运行。新工作区把用户选择原样传入 `init_workspace.py --game skyrim-se --tool-setup skip`，初始化后不重复工具准备。queue argv 必须同时含 `--mode prepare --mod-name ExampleMod --source-path mod/ExampleMod.zip --limit 1`，且不得无条件使用 `--mode workflow`。

- [ ] **Step 2: 写复用工作区测试**

auto 只做幂等工具验证/准备；manual 只检查并显示建议；skip 不调用工具入口。相同 identity 复用原 session/import，不复制覆盖；内容变化生成新 identity/workspace。额外 Mod 输入不吸收，影响 project readiness 时返回 `needs_user_input/3`。

- [ ] **Step 3: 实现 run**

顺序严格为：输入安全验证/manifest → resolve/reserve workspace → 初始化或复用工具处理 → 事务导入或已有导入验证 → 精确 queue prepare → CORE refresh → `advance_workflow()`。所有长任务在工作区独占 `smt-operation.lock` 下运行。

- [ ] **Step 4: 实现 resume**

解析显式/cwd/last workspace，验证 marker/session/import，获取独占锁，进入同一 `advance_workflow()`。GUI/Agent/user/普通 safe stop 返回 `3`；能力不支持 `4`；环境/工具不可用 `5`；timeout `124`；interrupt `130`。

- [ ] **Step 5: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py -k "run or resume or tool_setup"` 和 `python -m pytest -q tests/test_smt_cli_workspace.py -k "reuse or extra_mod or import"`。提交：`feat(cli): 编排 SMT 运行与安全恢复`。

---

### Task 7: 完成 status、doctor 与 output

**Files:**

- Modify: `scripts/smt_cli.py`
- Modify: `tests/test_smt_cli.py`
- Reuse: `scripts/project_paths.py:final_mod_dir`
- Reuse: `scripts/project_paths.py:intermediate_output_dir`
- Reuse: `scripts/project_paths.py:packaged_mod_path`

- [ ] **Step 1: 写 status 测试**

blocked/qa_failed 快照读取成功仍退出 `0`；`state_snapshot=True`、`refreshed_by_this_command=False`；state time 原样返回且 timezone 为 null。共享锁超时返回 `1`、`busy=True`，并证明 reader 未运行。progress card 缺失返回 `1`，不得生成替代卡片。

- [ ] **Step 2: 写 doctor 纯诊断测试**

运行前后对工作区除 CLI 自有诊断日志外的路径/mtime/size/hash 做快照，必须完全不变。fake runner 收到 install/build/cleanup/refresh/session/cache mutation 即失败。无 workspace 时只扫描默认根直属子目录。

- [ ] **Step 3: 写 output 测试**

产物不存在仍退出 `0`，ArtifactInfo 标记 `exists=False`。details 分别包含“可以进入人工游戏测试”和“人工游戏测试已验证”。`--open` 只允许 root/final-mod/intermediate/package-directory；共享锁内验证路径，释放后打开；不存在、越界或 reparse 替换返回 `1` 且不打开。

- [ ] **Step 4: 实现三个命令**

status/output 短时共享锁读取一致快照，共享持有者不写锁 metadata。doctor 只读平台/cache/reservation/marker/session/import/tool configuration，可写独立日志但不修复。output 只通过现有 output path helpers 和 session mod_name 构造路径。

- [ ] **Step 5: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py -k "status or doctor or output or busy or artifact"`、`git diff --check`。提交：`feat(cli): 增加只读状态诊断与产物查询`。

---

### Task 8: 建立唯一公开 argparse 和渲染层

**Files:**

- Create: `scripts/smt.py`
- Modify: `tests/test_smt_cli.py`

**Public CLI:**

```text
python scripts\smt.py run <input> --game <id> [--workspace] [--workspace-root] [--tool-setup auto|manual|skip] [--timeout-seconds]
python scripts\smt.py status [--workspace]
python scripts\smt.py resume [--workspace] [--timeout-seconds]
python scripts\smt.py doctor [--workspace]
python scripts\smt.py output [--workspace] [--open root|final-mod|intermediate|package-directory]
```

- [ ] **Step 1: 写 help/argparse/JSON 测试**

subprocess 执行 `python scripts/smt.py --help` 返回 `0` 且列出五个命令。未知命令和非法 open 枚举返回 argparse `2`。patch `dispatch()` 返回 `empty_result()` 后，`--format json` stdout 必须能被一次 `json.loads()` 解析、恰好一行、stderr 为空。

- [ ] **Step 2: 写平台测试**

非 Windows执行 compileall、import、`--help` 成功；五个真实子命令返回固定 schema 结果和 exit `5`，不出现 Win32 import traceback。

- [ ] **Step 3: 实现 `smt.py`**

该文件只构建 parser/request、调用 `smt_cli.dispatch()`、渲染 result。JSON 单次 `json.dumps(result.to_payload(), ensure_ascii=False)` 写 stdout。text 显示 outcome/message、原始 progress card、next action、output paths、details/diagnostics；不得重写进度卡或把无自动任务描述为完成。

- [ ] **Step 4: 验证并提交**

运行 `python scripts/smt.py --help`、`python -m pytest -q tests/test_smt_cli.py -k "help or argparse or json or render or non_windows"`、`python -m compileall -q scripts`。提交：`feat(cli): 提供 SMT 唯一公开命令入口`。

---

### Task 9: 固化文档、Skills 和静态治理合同

**Files:**

- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Modify: `ADVANCED_USER_GUIDE.md`
- Modify: `developer_guide.md`
- Modify: `scripts/README.md`
- Modify: `AGENTS.md`
- Modify: `skills/skyrim-mod-chs-translation/SKILL.md`
- Modify: `skills/skyrim-mod-translation-orchestrator/SKILL.md`
- Modify: `skills/workflow-agent-orchestration/SKILL.md`
- Modify: `skills/workflow-policy-and-state/SKILL.md`
- Modify: `scripts/ci_validate_repo.py`
- Modify: `tests/test_smt_cli.py`

- [ ] **Step 1: 写静态失败测试**

README/USER_GUIDE 必须出现 `python scripts\smt.py`，不得要求普通用户组合 init/queue/resume/state/tasks 脚本。workflow policy 的 JSON 序列化文本不得包含 `scripts/smt.py`。顶层 Agent Skills 必须要求 `--format json`，并禁止自行组合底层入口。

- [ ] **Step 2: 更新用户文档**

普通用户只看到五命令、默认 Documents 工作区、默认新工作区、支持输入、公开 outcome/exit code、ready 与 completed 区别。高级/开发文档保留底层脚本但标明内部实现/诊断用途。

- [ ] **Step 3: 更新 Agent/Skill 合同**

首次调用 run JSON；完成语言或获授权 GUI 动作后调用 resume；状态/产物只用 status/output。运行期/恢复 Skill 继续使用现有状态机，不让 workflow task 指向 smt 外层 controller。

- [ ] **Step 4: 增加生产静态检查**

在 `ci_validate_repo.py --strict` 检查公开入口唯一性、workflow policy 非递归、两个测试文件被跟踪、`pyproject.toml package=false`。不引入 `public_control_entrypoints` 第一版配置字段。

- [ ] **Step 5: 验证并提交**

运行 `python -m pytest -q tests/test_smt_cli.py -k "public_docs or workflow_policy or agent"`、`python scripts/ci_validate_repo.py --strict`、`python -m pytest -q scripts/test_skill_effects.py`。提交：`docs(cli): 统一 SMT 用户与 Agent 入口`。

---

### Task 10: 增加无真实工具的效果回归与并发集成

**Files:**

- Modify: `scripts/run_effect_regression.py`
- Create: `samples/effect_regression/cases/smt-single-entry/case.json`
- Create: `samples/effect_regression/cases/smt-single-entry/fixtures/ExampleMod/Interface/translations/example_english.txt`
- Create: `samples/effect_regression/cases/smt-single-entry/expected/summary.json`
- Modify: `tests/test_smt_cli_workspace.py`

- [ ] **Step 1: 创建失败 fixture**

`case.json` 固定为 schema 1、type `smt-single-entry`、game `skyrim-se`、tool setup `skip`。运行 `python scripts/run_effect_regression.py --case smt-single-entry --ci`，预期因新 case type 或 expected snapshot 缺失失败。

- [ ] **Step 2: 实现稳定 collector**

runner 把 fixture 打临时 ZIP，注入 stub init/tool/process services，执行首次暂停、同一输入复用、内容变化新工作区。环境必须包含 `SKYRIM_CHS_NO_EXTERNAL_TOOLS=1`，每次 run 显式 skip。summary 只含相对路径、outcome、exit code、identity/workspace 是否复用和 `uses_real_tools=false`，不含绝对路径、PID、时间。

- [ ] **Step 3: 生成并检查 expected**

运行 `python scripts/run_effect_regression.py --case smt-single-entry --update-expected`，人工确认稳定字段后再运行 `--ci`，预期通过。

- [ ] **Step 4: 增加并发集成**

两个 spawn 进程以不同 identity 和 stub 初始化器运行，barrier 证明初始化区间重叠；global state JSON 保持有效。相同 identity 的第二进程只能等待后复用或 timeout `6`，不得产生第二个 committed session。

- [ ] **Step 5: 验证并提交**

运行 `python scripts/run_effect_regression.py --case smt-single-entry --ci` 和 `python -m pytest -q tests/test_smt_cli_workspace.py -k "concurrent or reservation"`。提交：`test(cli): 增加唯一入口效果与并发回归`。

---

### Task 11: 完整验证、OpenSpec 跟踪与独立审查

**Files:**

- Modify: `openspec/changes/add-smt-single-user-entry/tasks.md`
- Create or Modify: 当前仓库治理合同指定的高风险独立审查证据文件
- Review: planning baseline 之后的全部变更

- [ ] **Step 1: 运行定向验证**

依次运行：

```powershell
python -m pytest -q tests/test_smt_cli.py tests/test_smt_cli_workspace.py
python -m compileall -q scripts
python scripts/ci_validate_repo.py --strict
python scripts/test_workflow_health.py --repo-only --strict
python -m pytest -q tests/test_workflow_refresh.py tests/test_project_paths.py
```

预期全部退出 `0`。

- [ ] **Step 2: 运行五类 required checks 本地等价入口**

static/windows-smoke：`python -m pytest -q tests` 加 CI 中列出的 repo-only、workflow、Skill、profile 测试。windows-fallout4-adapters：运行 `.github/workflows/ci.yml` 对应 dotnet 与 Python adapter 测试。windows-fallout4-workflow：运行 `python -m pytest -q scripts/test_fallout4_workflow_integration.py`。effect-regression：运行 `python scripts/run_effect_regression.py --all --ci`。本地全绿后仍须等待 GitHub 五个同名 job 对最新 SHA 全绿。

- [ ] **Step 3: 同步并验证 OpenSpec**

只有有测试/检查证据的 `openspec/changes/add-smt-single-user-entry/tasks.md` 项才能改为 `[x]`。运行 `openspec validate add-smt-single-user-entry` 和 `openspec status --change add-smt-single-user-entry`，预期 change valid 且 planning artifacts complete。

- [ ] **Step 4: 最新 commit 独立高风险审查**

审查范围必须包括路径/reparse、ZIP/7Z、不可变 session、reservation 锁序、Win32 handle、Job Object、workflow policy 非递归、状态投影、JSON/退出码、doctor 只读、effect 无真实工具。记录 reviewed SHA。

- [ ] **Step 5: 修复后重新审查**

任何修复形成新 commit 后，重新执行 Step 1–3，并让独立 Agent 审查新 SHA；旧 reviewed SHA 不得沿用。

- [ ] **Step 6: 最终检查**

运行 `git diff --check`、`git status --short`、`git log -1 --oneline`。预期无未提交实现文件，OpenSpec/evidence 对应最新 commit，GitHub required checks 全绿且审查对话已解决。

---

## Final Self-Review Checklist

- [ ] 每个 OpenSpec Requirement 至少有一个测试或静态合同。
- [ ] `classify_outcome()` 能返回 `None`，自动任务不会被 Agent/GUI 结果提前截断。
- [ ] completed/ready 同时检查 project/current Mod/global blocker。
- [ ] 精确任务只用当前 Mod + task_id，不使用 `run_workflow_tasks.py --limit 1`。
- [ ] `smt.py` 未进入 workflow policy 授权面或 workflow tasks。
- [ ] session no-replace，cache 非权威，多个匹配不会静默选择。
- [ ] 持有 global lock 时没有等待 reservation/workspace lock 的路径。
- [ ] 目录源复制后重新计算完整 manifest/digest，归档源重新哈希并再次验证 identity。
- [ ] status/output 使用共享锁；doctor 无 workflow/session/cache/tool 副作用。
- [ ] JSON 单对象、字段恒定、时间不伪造时区。
- [ ] 非 Windows compile/import/help 成功，真实命令返回 `5`。
- [ ] effect regression 显式 skip 且证明没有真实工具调用。
- [ ] `.gitignore` 只解除两个新测试文件。
- [ ] OpenSpec、定向测试、五类 required checks、最新 SHA 独立审查均有证据。
