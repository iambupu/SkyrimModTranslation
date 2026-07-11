---
name: bsa-archive-audit
description: "用于处理 Skyrim Mod 的 BSA/BA2 归档审计。中文触发：BSA、BA2、归档、归档审计、检查 BSA 里有没有文本、BSA 解包、loose override、归档缺口、archive blocker。Use for workspace-local .bsa/.ba2 audits, translatable resource classification, safe BSAFileExtractor wrapper, archive manifests, and BSA/BA2 QA blockers. Do not use for RAR, plugin editing, translation, GUI tools, BA2 extraction, or repacking archives."
---

# BSA Archive Audit

## Goal

Handle `.bsa/.ba2` as auditable archive boundaries. This Skill reads workspace-local BSA/BA2 archives only to produce evidence for routing and QA. BSA may be extracted through the plugin safe wrapper when materialization is required; BA2 is read-only audit only until a separate BA2 extractor adapter exists. This Skill does not translate files, edit archives, repack BSA, or install anything into Skyrim/MO2/Vortex.

## Inputs

- `mod/**/*.bsa`
- `mod/**/*.ba2`
- `work/extracted_mods/<ModName>/**/*.bsa`
- `work/extracted_mods/<ModName>/**/*.ba2`
- `config/tools.local.json`
- `qa/decoder_tools_report.md`

## Outputs

- `work/archive_extracts/<ModName>/<ArchiveName>/`
- `out/<ModName>/archive_audits/<ArchiveName>/manifest.json`
- `out/<ModName>/archive_audits/<ArchiveName>/files.jsonl`
- `qa/<ModName>.<ArchiveName>.archive_audit_manifest.md`
- `qa/<ModName>.archive_coverage.md`
- `qa/<ModName>.archive_loose_override_exemptions.jsonl` when a translatable archive entry is intentionally not delivered as loose override

## Tool Priority

1. `scripts/new_bsa_archive_manifest.py` with `bethesda-structs` for read-only BSA/BA2 archive inventory, candidate classification, and manifest evidence.
2. `scripts/invoke_bsa_file_extractor_safe.py` when actual BSA extraction is required.
3. `scripts/new_archive_audit_manifest.py` only after a workspace-local extraction directory exists.
4. Blocked report if neither read-only audit evidence nor safe extraction can be produced.

`BSAFileExtractor.py` is not called directly. Use only the plugin wrapper configured by `DecoderTools.BsaFileExtractorPath`; the wrapper must keep input under the workspace root and output under `work/archive_extracts/`. Do not use the BSA extractor wrapper for BA2.

## Workflow

1. Confirm the `.bsa/.ba2` path is inside the workspace and normally comes from `mod/` or `work/extracted_mods/<ModName>/`.
2. Run or inspect `python scripts/detect_decoder_tools.py --config-path config/tools.local.json`.
3. If `bethesda-structs` is ready, generate read-only archive inventory and content classification. Do not extract just to prove that an archive exists:

```console
python scripts/new_bsa_archive_manifest.py --mod-name <ModName> --archive-path <workspace-local-bsa>
```

4. If BSA files must be materialized, run:

```console
python scripts/invoke_bsa_file_extractor_safe.py --archive-path <workspace-local-bsa> --output-dir work/archive_extracts/<ModName>/<ArchiveName>
```

5. If BSA extraction was required, generate or refresh the extraction-backed manifest:

```console
python scripts/new_archive_audit_manifest.py --mod-name <ModName> --archive-path <workspace-local-bsa> --extracted-dir work/archive_extracts/<ModName>/<ArchiveName> --force
```

6. Run archive coverage before claiming final_mod completeness:

```console
python scripts/audit_archive_coverage.py --mod-name <ModName>
```

In strict completion, every manifest row with `Risk=translatable` must either exist in `final_mod/` at the same archive-relative path or be listed in `qa/<ModName>.archive_loose_override_exemptions.jsonl`.

Each exemption row is JSONL:

```json
{"Archive":"Example.bsa","RelativePath":"Interface/translations/foo_english.txt","Status":"accepted","Reason":"No player-visible strings after manual archive review.","Reviewer":"Agent model"}
```

Use `Archive` as the archive filename or workspace-relative archive path. `Status` must be `accepted`, `approved`, or `exempted`; `Reason` and `Reviewer` are required. Optional `EvidencePath` must point to an existing workspace-local file.

## Classification Rules

- `translatable`: Interface translation files, JSON, XML, INI, TXT, STRINGS-family files after an appropriate decoder exists.
- `decoder-required`: PEX, STRINGS/DLSTRINGS/ILSTRINGS, SWF/GFX, or any format that needs a dedicated reader before visible text can be proven.
- `manual-review`: meshes, textures, animations, audio, binary resources, and ambiguous files.

Do not translate inside this Skill. Route extracted text resources to `text-resource-translation`, plugin records to `esp-esm-esl-translation`, and PEX/PSC evidence to `pex-visible-strings-translation`.
If an archive entry under `Interface/translations/*.txt` is routed as translatable, downstream delivery must preserve the original archive-relative path and pass the final Interface runtime audit as UTF-16 LE BOM `$key<TAB>value` text.

## Delivery Policy

Translated content discovered inside a BSA/BA2 is delivered as same-path loose override by default, not by repacking the archive. Preserve the archive-internal relative path and original file name when handing work to downstream Skills:

```text
work/archive_extracts/<ModName>/<ArchiveName>/Interface/translations/foo_english.txt
-> translated/final_mod/<ModName>/Interface/translations/foo_english.txt
-> out/<ModName>/汉化产出/final_mod/Interface/translations/foo_english.txt
```

The original `.bsa/.ba2` remains an unchanged file in `final_mod/`. BSA repacking is a high-risk future adapter path only when manual game testing proves same-path loose override does not load or causes a Mod-specific issue. BA2 repacking/extraction is outside the current default flow until a separate controlled BA2 adapter exists.
For `Interface/translations/*.txt` loose overrides, final delivery must also satisfy `qa/<ModName>.final_interface_runtime.md` with zero blocking issues and zero warnings; archive coverage alone is not enough to prove the MCM text can load.

## Safety Rules

- Do not modify, delete, overwrite, repack, or optimize `.bsa/.ba2`.
- Do not write loose extracted files into `mod/`, `final_mod/`, real Skyrim, MO2, Vortex, Steam, AppData, or Documents/My Games paths.
- Do not treat raw extracted loose files as final delivery. Only translated, QA-reviewed, same-path loose overrides assembled by `final-mod-assembly` may enter `final_mod/`.
- Do not repack BSA unless a future controlled packer adapter, manifest, hash verification, and manual-test evidence explicitly require it.
- Do not claim complete localization if a BSA/BA2 exists and no workspace-local audit manifest exists.
- Do not extract or repack `.ba2`; BA2 still needs a separate adapter for materialization or writeback.

## Done When

- The BSA/BA2 path and any extraction output are workspace-local.
- `qa/decoder_tools_report.md` shows `bethesda-structs` ready, or the lack of support is recorded as blocked.
- `out/<ModName>/archive_audits/<ArchiveName>/manifest.json` exists or a blocked reason is written.
- `qa/<ModName>.archive_coverage.md` records the BSA coverage status.
- Any translated BSA/BA2 content is routed as same-path loose override, or a blocked reason explains why a future archive packer/extractor adapter is required.
- Every `Risk=translatable` manifest row has a same-path loose override in `final_mod/`, or a valid exemption row records why that archive entry is intentionally not delivered.
- Any delivered `Interface/translations/*.txt` loose override has passed final Interface runtime audit, not only archive coverage.
- No step edited or repacked the source BSA.
