---
name: ba2-archive-audit
description: "用于 Fallout 4 工作区 BA2 只读审计、安全解包、receipt/manifest/hash 验证和同路径 loose override 证据。中文触发：BA2、Fallout 4 归档、BA2 解包、BA2 manifest、loose override。Use for workspace-local .ba2 inventory, controlled extraction through the BA2 adapter protocol, extraction verification, archive coverage, and loose-override provenance. Never edit or repack BA2 archives."
---

# BA2 Archive Audit

## Scope

This Skill handles workspace-local Fallout 4 `.ba2` archives. It may inventory an archive read-only, or materialize files through the controlled BA2 wrapper. It does not translate inside the archive, edit the source BA2, repack BA2, or access a real Fallout 4, MO2, Vortex, Steam, AppData, or Documents/My Games directory.

BA2 delivery is always `allow_repack=false`（不重打包）. Translated content is delivered as a same-path loose override after extraction evidence and downstream translation QA pass.

## Controlled Adapter Protocol

`DecoderTools.Ba2ExtractorPath` must point to a reviewed adapter file inside the current workspace or plugin root. `DecoderTools.Ba2ExtractorProtocol` must be:

```text
skyrim-mod-chs.ba2-extractor.v1
```

The safe wrapper starts the adapter with exactly these public arguments:

```text
--archive-path <absolute-workspace-ba2> --output-dir <isolated-staging-payload-dir>
```

A `.py` adapter is launched with the current Python interpreter. Other adapter files are launched directly. Do not place raw third-party CLI arguments in the wrapper; create a reviewed adapter around that tool instead.

The external process is not an operating-system sandbox. The wrapper detects source BA2 changes, links, hardlinks, path escapes materialized in the payload, and writes beside the payload inside its staging root. It cannot prove that a hostile executable made no arbitrary write elsewhere. Only reviewed workspace/plugin adapters qualify for this protocol.

## Workflow

1. Confirm the archive is an existing `.ba2` under `mod/` or `work/extracted_mods/`.
2. Run decoder detection. A file path without the exact protocol marker and controlled path scope remains blocked:

```console
python scripts/detect_decoder_tools.py --config-path config/tools.local.json
```

3. Use `bethesda-structs` inventory for read-only classification when extraction is not needed.
4. When materialization is required, use only the safe wrapper and the exact output contract:

```console
python scripts/invoke_ba2_extractor_safe.py --mod-name <ModName> --archive-path <workspace-ba2> --output-dir work/archive_extracts/<ModName>/<ArchiveName>
```

5. Independently verify the receipt, source archive hash, adapter identity, manifest, files JSONL, path contract, links, limits, file hashes, and sizes:

```console
python scripts/verify_ba2_extraction.py --manifest-path out/<ModName>/archive_audits/<ArchiveName>/manifest.json
```

6. `new_ba2_archive_manifest.py` may refresh an existing extraction only when the wrapper-generated `extraction_receipt.json` remains valid. It cannot create source-unchanged or atomic-publish claims from two snapshots of the same later state.
7. Route extracted text to the appropriate translation Skill. Preserve the archive-relative path under `translated/final_mod/<ModName>/`.
8. Record BA2 loose provenance in `out/<ModName>/archive_audits/ba2_loose_overrides.jsonl`, then run archive coverage and final assembly.

Each sidecar row contains `ManifestPath`, `ArchivePath`, `EntryPath`, `OverlayPath`, and `SourceSha256`. `OverlayPath` must be exactly `translated/final_mod/<ModName>/<EntryPath>`.

## Evidence

- `work/archive_extracts/<ModName>/<ArchiveName>/`
- `out/<ModName>/archive_audits/<ArchiveName>/extraction_receipt.json`
- `out/<ModName>/archive_audits/<ArchiveName>/manifest.json`
- `out/<ModName>/archive_audits/<ArchiveName>/files.jsonl`
- `out/<ModName>/archive_audits/ba2_loose_overrides.jsonl`
- `qa/<ModName>.archive_coverage.md`
- `out/<ModName>/汉化产出/final_mod/meta/provenance.jsonl`

## Fail Closed

- Reject missing/wrong protocol, external adapter paths, unsafe archive/output layouts, nonempty targets, source archive changes, adapter failures, staging siblings, links/reparse points, hardlinks, path traversal, reserved names, NUL, and configured limits exceeded.
- Remove the staging directory, newly published extraction, and the archive's stale/current evidence directory on any wrapper failure.
- A bethesda-structs inventory proves read-only inspection only. It does not authorize materialization or BA2-derived loose provenance.
- Never copy raw extracted files directly into `final_mod/`; only reviewed files already under `translated/final_mod/<ModName>/` may be assembled.
- Never modify or repack a BA2 archive.

## Done When

- Source BA2 hash/size match the wrapper receipt and manifest.
- Adapter identity and protocol still match current files/configuration.
- Independent verification passes with no missing, extra, moved, linked, oversized, or hash-drifted entry.
- Every translatable BA2 entry has a same-path loose override or an accepted archive coverage exemption.
- Final provenance identifies the BA2 archive, entry path/hash, extraction manifest, and final loose file.
- `allow_repack=false` remains explicit throughout the evidence chain.
