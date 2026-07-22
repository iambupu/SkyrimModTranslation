## 变更说明

<!-- 说明解决的问题、改动边界以及未包含的内容。 -->

## 风险与审查

- [ ] 分支基于最新 `master`，合并前已再次同步。
- [ ] 已确认本 PR 是否涉及下列高风险范围：adapter、QA、release、路径安全、归档处理、工作流门禁。
- [ ] 若涉及高风险范围，已在最新提交上请求独立 Agent 审查，并记录 reviewed commit。
- [ ] 所有审查对话均已解决；没有用新 PR 绕过旧 PR 的未解决线程。
- [ ] 若为堆叠 PR，已填写直接父 PR，且父层合并后会立即 rebase/retarget；若 `ahead_by=0`，将关闭为 `superseded`。

独立审查记录：

```text
Reviewer:
Reviewed commit:
Findings resolved by:
```

## 验证

- [ ] Static repository validation（Python 3.11）
- [ ] Static repository validation（Python 3.12）
- [ ] Windows repository smoke
- [ ] Windows Fallout 4 adapters
- [ ] Windows Fallout 4 workflow
- [ ] Effect regression fixtures

补充验证：

```text
<commands and results>
```

## 堆叠 PR 信息

- 直接父 PR：无
- 父 PR 状态：不适用
- 父层合并后的动作：不适用 / rebase + retarget / superseded + close
