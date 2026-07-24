---
name: managed-tool-cache-maintenance
description: Inspect, clean unused generations, or fully uninstall the machine-shared SMT managed-tool cache. Use only when the user explicitly asks to inspect cache usage, release disk space, clean old managed tool versions, or uninstall shared managed tools; never use during ordinary Mod translation, setup, recovery, QA, upgrade, or release. 中文触发：缓存检查、清理未使用工具、卸载共享工具。
---

# Managed Tool Cache Maintenance

Use this Skill only for explicit shared-cache maintenance. The cache is
rebuildable, but its entries can be in active use by several workspaces.
Workspaces, Mod inputs, translations, QA evidence, manual external tools, and
the control/lock namespace are never deletion targets.

This is a Windows runtime Skill. All commands shown here use Windows
PowerShell syntax, while deletion remains encapsulated in the controlled Python
maintenance engine.

## Hard boundaries

- Run only `python scripts/manage_managed_tool_cache.py` from the installed
  plugin source using the bootstrap Python process.
- Never run this entry from a managed Python runtime that the plan may remove.
- Never use PowerShell, `cmd`, Python snippets, filesystem tools, or shell
  deletion to remove cache files.
- Never accept or synthesize an arbitrary deletion path.
- Never edit `catalog.json`, a workspace binding, a plan, a result, or workflow
  policy directly.
- Never invoke cache removal from setup, doctor, translation, resume, QA,
  release, upgrade, or workspace deletion.
- Treat catalog coverage as `known-registered-only`; moved, copied, offline, or
  never-reregistered workspaces may not be represented.

## Inspect

Run:

```powershell
python scripts\manage_managed_tool_cache.py inspect
```

Report entry identities, health, references, logical bytes, busy leases,
staging/trash remnants, and the discovery limitation. Inspection is strictly
read-only and must not create an absent store. Treat each reference's `status`
as durable catalog state and `observed_classification` as the current read-only
`valid`/`stale` observation. Inspection never rewrites durable status; only an
explicitly approved replacement cleanup plan may release an observed-stale
reference.

A released schema-v2 marker may legitimately predate `workspace_id`; the
engine must use a safely read matching immutable SMT session as identity
evidence and keep a matching binding `valid`. If marker/session evidence is
invalid or conflicting, report that diagnostic and retain the reference
conservatively; never infer identity from the workspace path or recommend
release merely because the marker lacks the newer field.

## Clean unused generations

1. Run:

   ```powershell
   python scripts\manage_managed_tool_cache.py plan-clean-unused
   ```

2. Show the complete returned plan to the user, including every included and
   retained entry, reference, effect, logical byte count, expiration time,
   atomicity policy, and confirmation token.
3. Stop and wait for explicit confirmation of that exact plan. Do not interpret
   the original cleanup request as confirmation of a newly generated plan.
4. If the user explicitly decides to release a listed stale reference, create a
   new plan with one exact
   `--release-stale-reference <ReferenceId>` argument per approved reference,
   show the replacement plan, and wait for confirmation again.
5. Apply only the confirmed plan:

   ```powershell
   python scripts\manage_managed_tool_cache.py apply-plan --plan-id <PlanId> --confirmation-token <Token>
   ```

Unused cleanup is best effort per entry. A changed, busy, invalid, or newly
referenced entry is retained and the result may be `partial`.

## Full uninstall

1. Run:

   ```powershell
   python scripts\manage_managed_tool_cache.py plan-uninstall
   ```

2. Explain that full uninstall intentionally invalidates every listed derived
   workspace binding until a later `--tool-setup auto` run reinstalls/rebinds
   tools. Workspaces and their content remain untouched.
3. Show the complete plan and its `known-registered-only` warning.
4. Stop and wait for explicit confirmation of that exact plan.
5. Apply it with `apply-plan` and the exact plan ID/token.

Full uninstall has an all-or-nothing eligibility/detach transaction. If any
entry is busy, changed, invalid, or cannot be locked, no planned payload entry
is detached. A filesystem failure after the atomic detach can still leave
plan-scoped trash; that is `interrupted`, not a completed uninstall, and must
be reported by the post-check.

## Mandatory post-check

After every apply attempt, run `inspect` again. Report both the apply result and
post-operation inspection. Do not claim completion when the result is
`partial`, `blocked`, `interrupted`, mismatched, expired, or when trash
remnants remain. The control root, locks, plan, and result must remain readable
after payload uninstall.
