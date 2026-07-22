# 仓库合并与审查规则

本文规定 `master`、高风险改动、hotfix 和堆叠 PR 的维护流程。GitHub Branch Protection 是合并事实来源；本文和 PR 模板用于补充单人维护仓库无法由 GitHub 原生表达的独立 Agent 审查要求。

## master 门禁

`master` 必须启用以下保护：

- 禁止直接 push，管理员也不能绕过。
- 所有改动必须通过 PR。
- PR 分支必须与最新 `master` 同步。
- 必须解决全部审查对话。
- 禁止 force push 和删除 `master`。
- 只允许 squash merge，并要求线性历史。
- 合并后自动删除远程 head 分支。

以下 CI context 必须全部成功：

1. `Static repository validation (Python 3.11)`
2. `Static repository validation (Python 3.12)`
3. `Windows repository smoke`
4. `Windows Fallout 4 adapters`
5. `Windows Fallout 4 workflow`
6. `Effect regression fixtures`

CI 是合并前门禁，不得先合入再等待结果。重新 push 后必须等待最新提交对应的检查，不能复用旧提交结果。

## 单人维护与独立审查

仓库当前只有一名具备写权限的维护者。GitHub 不允许 PR 作者批准自己的 PR，因此 `required_approving_review_count` 保持为 `0`；否则所有 PR 都会永久无法合并。

这不取消独立审查要求。下列高风险范围必须在最新提交上请求独立 Agent 审查：

- `adapters/**` 及其测试。
- `.github/workflows/**`、发布脚本和 release metadata。
- QA、strict gate、used capabilities、provenance、final_mod 和交付验证。
- 路径解析、reparse point、Windows 短路径、归档 entry 和目录边界。
- BSA、BA2、ZIP、7Z 的清点、物化、验证和覆盖包组装。
- Game Profile、capability、workflow policy、adapter contract 和二进制写回边界。

PR 必须记录 reviewer、reviewed commit 和 findings 的解决提交。更新 reviewed commit 后，应重新请求审查。Codex Review 产生的所有线程都必须解决；没有建议时也应保留已审查最新提交的证据。

如果独立 Agent 不可用，高风险 PR 应保持未合并，而不是把自审描述成二次审查。未来增加第二名具备写权限的协作者或审查 App 后，应把 `required_approving_review_count` 提升为 `1`。

## Hotfix

紧急修复不豁免门禁：

1. 从最新 `master` 创建 `hotfix/<short-description>`。
2. 只提交修复和对应回归测试，避免夹带重构。
3. 创建 PR，并等待全部 required checks 完成。
4. 高风险 hotfix 在最新提交上完成独立 Agent 审查。
5. 解决全部审查对话，确认分支仍与 `master` 同步。
6. 使用 squash merge；不得直接 push、临时关闭保护或先合入后验证。

## 堆叠 PR

堆叠 PR 只用于确实需要分层审查的变更，并遵守以下规则：

- 每个子 PR 的 base 必须是直接父分支；PR 正文必须记录父 PR。
- 必须从底层到顶层依次合并，不得跳层合并。
- 父 PR 合并后，所有开放子 PR 必须立即处理：
  - 仍有独立提交：rebase 到最新 `master`，retarget 到 `master`，重新运行 CI 和审查。
  - `ahead_by=0`：添加 `superseded` 标签，说明内容由哪个 PR/current master 吸收，然后关闭。
- 不得让子 PR 长期指向已经合并或删除的父分支。
- 关闭 superseded PR 后，删除 GitHub 和 Gitee 的对应远程 head 分支。
- 删除前必须确认该分支已被 `master` 包含，并记录 `ahead_by=0`；不能仅凭文件看起来相同就删除。

## 合并后检查

- 回读 PR 状态、合并方式和最终提交。
- 确认 required checks 在合并前完成，而不是合并后补跑。
- 确认没有未解决审查线程。
- 确认 head 分支已删除，堆叠子 PR 已 rebase/retarget 或关闭。
- GitHub/Gitee 双远程存在时，分别核对需要同步的分支；不得 force push 消除平台生成的不同合并哈希。
