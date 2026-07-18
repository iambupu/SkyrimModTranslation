from __future__ import annotations

import copy
import json
import sys
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import game_context  # noqa: E402
import plugin_resource_evidence  # noqa: E402


KNOWN_CATEGORIES = {
    "archive",
    "interface",
    "loose_text",
    "package",
    "papyrus",
    "plugin",
    "protected_binary",
    "string_table",
}


def resource_model_payload() -> dict[str, object]:
    return {
        "extension_groups": [
            {
                "name": "plugin",
                "category": "plugin",
                "extensions": [".esp", ".esm", ".esl"],
                "capability": "plugin_text",
                "default_traits": {".esl": ["light"]},
            },
            {
                "name": "archive.ba2",
                "category": "archive",
                "extensions": [".ba2"],
                "capability": "archive.ba2",
            },
            {
                "name": "archive.bsa",
                "category": "archive",
                "extensions": [".bsa"],
                "capability": "archive.bsa",
            },
            {
                "name": "string_table",
                "category": "string_table",
                "extensions": [".strings", ".dlstrings", ".ilstrings"],
                "capability": "string_tables",
            },
            {
                "name": "papyrus.binary",
                "category": "papyrus",
                "extensions": [".pex"],
                "capability": "pex",
            },
            {
                "name": "papyrus.source",
                "category": "papyrus",
                "extensions": [".psc"],
                "capability": "loose_text",
            },
            {
                "name": "interface.binary",
                "category": "interface",
                "extensions": [".swf", ".gfx"],
                "capability": "",
            },
            {
                "name": "loose_text",
                "category": "loose_text",
                "extensions": [
                    ".txt",
                    ".xml",
                    ".json",
                    ".jsonl",
                    ".csv",
                    ".md",
                ],
                "capability": "loose_text",
            },
            {
                "name": "config_text",
                "category": "loose_text",
                "extensions": [".ini", ".toml"],
                "capability": "loose_text",
            },
            {
                "name": "package",
                "category": "package",
                "extensions": [".zip", ".7z", ".rar"],
                "capability": "",
            },
            {
                "name": "protected_binary",
                "category": "protected_binary",
                "extensions": [
                    ".nif",
                    ".dds",
                    ".bgsm",
                    ".bgem",
                    ".dll",
                    ".exe",
                ],
                "capability": "",
            },
        ],
        "containers": {
            "interface": "interface",
            "scripts": "papyrus",
            "f4se": "f4se",
            "mcm": "mcm",
            "strings": "string_table",
            "meshes": "protected",
            "textures": "protected",
            "materials": "protected",
            "sound": "protected",
            "music": "protected",
            "video": "protected",
            "vis": "protected",
            "seq": "protected",
        },
        "trait_level_caps": {
            "plugin_text": {
                "localized": "inventory_only",
                "light": "read_only",
                "contains_unsupported_light_formids": "read_only",
            }
        },
    }


def valid_profile_payload() -> dict[str, object]:
    payload = json.loads(
        (ROOT / "config" / "game_profiles" / "fallout4.json").read_text(
            encoding="utf-8"
        )
    )
    model = resource_model_payload()
    payload["resource_model"] = model
    containers = model["containers"]
    assert isinstance(containers, dict)
    payload["data_directories"] = list(containers)
    payload["protected_directories"] = [
        name for name, container in containers.items() if container == "protected"
    ]
    return payload


def load_profile_from_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> game_context.GameContext:
    profile_dir = tmp_path / "config" / "game_profiles"
    profile_dir.mkdir(parents=True)
    (profile_dir / "fallout4.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(game_context.PLUGIN_ROOT_ENV, str(tmp_path))
    return game_context.load_game_profile("fallout4")


def test_repository_profiles_expose_immutable_resource_models() -> None:
    fallout4 = game_context.load_game_profile("fallout4")
    skyrim = game_context.load_game_profile("skyrim-se")

    assert fallout4.resource_model.extension_groups
    assert skyrim.resource_model.extension_groups
    assert set(group.category for group in fallout4.resource_model.extension_groups) <= KNOWN_CATEGORIES
    assert fallout4.resource_model.trait_level_caps["plugin_text"] == {
        "localized": "inventory_only",
        "light": "experimental_write",
        "contains_unsupported_light_formids": "read_only",
    }
    plugin_group = next(
        group
        for group in fallout4.resource_model.extension_groups
        if group.category == "plugin"
    )
    assert plugin_group.default_traits == {".esl": frozenset({"light"})}
    assert skyrim.resource_model.trait_level_caps["plugin_text"] == {
        "light": "experimental_write",
    }
    assert isinstance(fallout4.resource_model.extension_groups[0].extensions, frozenset)
    with pytest.raises(FrozenInstanceError):
        fallout4.resource_model.extension_groups[0].name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        fallout4.resource_model.containers["new"] = "protected"  # type: ignore[index]
    with pytest.raises(TypeError):
        fallout4.resource_model.trait_level_caps["plugin_text"]["light"] = "stable"  # type: ignore[index]
    with pytest.raises(TypeError):
        plugin_group.default_traits[".esl"] = frozenset()  # type: ignore[index]


def test_unknown_plugin_write_traits_are_derived_from_profile_caps() -> None:
    skyrim = game_context.load_game_profile("skyrim-se")
    unknown_traits = plugin_resource_evidence.PluginReportTraits()

    assert plugin_resource_evidence.required_known_plugin_trait_fields(skyrim) == (
        "light_by_header",
    )
    assert plugin_resource_evidence.unknown_write_plugin_trait_fields(
        skyrim, unknown_traits
    ) == ("light_by_header",)

    synthetic_model = replace(
        skyrim.resource_model,
        trait_level_caps={
            "plugin_text": {
                "localized": "inventory_only",
                "light": "read_only",
                "contains_unsupported_light_formids": "read_only",
            }
        },
    )
    synthetic = replace(
        skyrim,
        game_id="synthetic-profile",
        resource_model=synthetic_model,
    )

    assert plugin_resource_evidence.required_known_plugin_trait_fields(synthetic) == (
        "localized",
        "light_by_header",
        "contains_unsupported_light_formids",
    )
    assert plugin_resource_evidence.unknown_write_plugin_trait_fields(
        synthetic,
        unknown_traits,
    ) == (
        "localized",
        "light_by_header",
        "contains_unsupported_light_formids",
    )


@pytest.mark.parametrize(
    ("relative_path", "category", "subtype", "container", "capability"),
    [
        ("Example.esp", "plugin", "plugin", "", "plugin_text"),
        ("Example.esm", "plugin", "plugin", "", "plugin_text"),
        ("Example.esl", "plugin", "plugin", "", "plugin_text"),
        ("Example.ba2", "archive", "archive.ba2", "", "archive.ba2"),
        ("Example.bsa", "archive", "archive.bsa", "", "archive.bsa"),
        ("Strings/Example.STRINGS", "string_table", "string_table", "string_table", "string_tables"),
        ("Scripts/Example.pex", "papyrus", "papyrus.binary", "papyrus", "pex"),
        ("Scripts/Source/Example.psc", "papyrus", "papyrus.source", "papyrus", "loose_text"),
        ("Interface/Example.swf", "interface", "interface.binary", "interface", ""),
        ("INTERFACE/Example.GFX", "interface", "interface.binary", "interface", ""),
        ("MCM/Config/X/config.json", "loose_text", "loose_text", "mcm", "loose_text"),
        ("F4SE/Plugins/X.dll", "protected_binary", "protected_binary", "f4se", ""),
        ("Materials/X.bgsm", "protected_binary", "protected_binary", "protected", ""),
        ("Meshes/MCM/config.json", "loose_text", "loose_text", "protected", "loose_text"),
        ("Materials/Scripts/foo.pex", "papyrus", "papyrus.binary", "protected", "pex"),
        ("F4SE/MCM/config.json", "loose_text", "loose_text", "f4se", "loose_text"),
        ("Config/Example.ini", "loose_text", "config_text", "", "loose_text"),
        ("Config/Example.toml", "loose_text", "config_text", "", "loose_text"),
        ("Package.zip", "package", "package", "", ""),
        ("Package.7Z", "package", "package", "", ""),
        ("Package.rar", "package", "package", "", ""),
        ("Unknown.bin", "unknown", "unknown", "", ""),
    ],
)
def test_classify_resource_matrix(
    relative_path: str,
    category: str,
    subtype: str,
    container: str,
    capability: str,
) -> None:
    from resource_model import classify_resource

    context = game_context.load_game_profile("fallout4")
    descriptor = classify_resource(context, Path(relative_path))

    assert descriptor.relative_path == Path(relative_path)
    assert descriptor.category == category
    assert descriptor.subtype == subtype
    assert descriptor.container == container
    assert descriptor.extension == Path(relative_path).suffix.lower()
    assert descriptor.capability == capability
    expected_traits = frozenset({"light"}) if relative_path.lower().endswith(".esl") else frozenset()
    assert descriptor.traits == expected_traits


def test_classify_resource_preserves_traits_and_uses_profile_extensions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resource_model import classify_resource

    payload = valid_profile_payload()
    model = payload["resource_model"]
    assert isinstance(model, dict)
    groups = model["extension_groups"]
    assert isinstance(groups, list)
    loose_text = next(group for group in groups if group["name"] == "loose_text")
    loose_text["extensions"].append(".custom")
    context = load_profile_from_payload(tmp_path, monkeypatch, payload)

    descriptor = classify_resource(
        context,
        Path("MCM/Config/Value.CUSTOM"),
        traits=frozenset({"localized", "light"}),
    )

    assert descriptor.category == "loose_text"
    assert descriptor.container == "mcm"
    assert descriptor.capability == "loose_text"
    assert descriptor.traits == frozenset({"localized", "light"})


def test_classify_resource_merges_profile_default_and_adapter_traits() -> None:
    from resource_model import classify_resource

    context = game_context.load_game_profile("fallout4")
    descriptor = classify_resource(
        context,
        Path("Data/Example.esl"),
        traits=frozenset({"localized"}),
    )

    assert descriptor.traits == frozenset({"light", "localized"})


@pytest.mark.parametrize("game_id", ["skyrim-se", "fallout4"])
def test_repository_profiles_classify_markdown_as_loose_text(game_id: str) -> None:
    from resource_model import classify_resource

    context = game_context.load_game_profile(game_id)
    descriptor = classify_resource(context, Path("Docs/Readme.md"))

    assert descriptor.category == "loose_text"
    assert descriptor.subtype == "loose_text"
    assert descriptor.capability == "loose_text"


@pytest.mark.parametrize(
    "relative_path",
    [
        ROOT / "absolute.esp",
        Path(""),
        Path("."),
        Path("Scripts/../Example.pex"),
    ],
)
def test_classify_resource_rejects_noncanonical_relative_paths(
    relative_path: Path,
) -> None:
    from resource_model import classify_resource

    context = game_context.load_game_profile("fallout4")

    with pytest.raises(ValueError, match="relative file path"):
        classify_resource(context, relative_path)


def model_mutation(mutator):
    payload = valid_profile_payload()
    mutator(payload)
    return payload


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda payload: payload["resource_model"].update(extension_groups=[]),
            "extension_groups.*non-empty",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(name=""),
            "name.*non-empty",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(extensions=[]),
            "extensions.*non-empty",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(category="audio"),
            "unknown category",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][1]["extensions"].append(".ESP"),
            "duplicate extension",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0]["extensions"].__setitem__(0, "ESP"),
            "canonical lowercase",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(capability="missing"),
            "capability.*missing",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(
                default_traits={".esp": ["Light"]}
            ),
            "default_traits.*canonical",
        ),
        (
            lambda payload: payload["resource_model"]["extension_groups"][0].update(
                default_traits={".missing": ["light"]}
            ),
            "default_traits.*extension",
        ),
        (
            lambda payload: payload["resource_model"]["trait_level_caps"]["plugin_text"].update(light="preview"),
            "trait level cap.*preview",
        ),
        (
            lambda payload: payload["resource_model"]["containers"].update(Interface="other"),
            "duplicate container key",
        ),
        (
            lambda payload: payload["resource_model"]["containers"].update(scripts="papryus"),
            "container value.*papryus",
        ),
    ],
)
def test_loader_rejects_invalid_resource_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutator,
    message: str,
) -> None:
    payload = model_mutation(mutator)

    with pytest.raises(ValueError, match=message):
        load_profile_from_payload(tmp_path, monkeypatch, payload)


def test_loader_derives_compatibility_resource_fields_from_resource_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = copy.deepcopy(valid_profile_payload())
    payload["plugin_extensions"] = [".drift"]
    payload["string_table_extensions"] = [".drift"]
    payload["data_directories"] = ["drift"]
    payload["protected_directories"] = ["drift"]

    context = load_profile_from_payload(tmp_path, monkeypatch, payload)

    assert context.plugin_extensions == frozenset({".esp", ".esm", ".esl"})
    assert context.string_table_extensions == frozenset(
        {".strings", ".dlstrings", ".ilstrings"}
    )
    assert context.data_directories == frozenset(context.resource_model.containers)
    assert context.protected_directories == frozenset(
        name
        for name, container in context.resource_model.containers.items()
        if container == "protected"
    )


@pytest.mark.parametrize("construction", ["direct", "replace"])
def test_game_context_defensively_copies_mutable_collections(construction: str) -> None:
    base = game_context.load_game_profile("fallout4")
    plugin_extensions = {".esp"}
    string_table_extensions = [".strings"]
    data_directories = {"interface"}
    protected_directories = ["meshes"]
    risky_paths = ["Fallout 4/Data"]
    consumers = {"rag"}
    glossary_source = game_context.GlossarySource(
        relative_path=Path("glossary/test.md"),
        format="markdown",
        consumers=consumers,
        recommended=True,
    )
    glossary_sources = [glossary_source]
    replacements = {
        "plugin_extensions": plugin_extensions,
        "string_table_extensions": string_table_extensions,
        "data_directories": data_directories,
        "protected_directories": protected_directories,
        "risky_paths": risky_paths,
        "glossary_sources": glossary_sources,
    }
    if construction == "direct":
        values = {field.name: getattr(base, field.name) for field in fields(base)}
        values.update(replacements)
        context = game_context.GameContext(**values)
    else:
        context = replace(base, **replacements)

    plugin_extensions.add(".esm")
    string_table_extensions.append(".dlstrings")
    data_directories.add("scripts")
    protected_directories.append("textures")
    risky_paths.append("Vortex")
    consumers.add("xtranslator")
    glossary_sources.clear()

    assert context.plugin_extensions == frozenset({".esp"})
    assert context.string_table_extensions == frozenset({".strings"})
    assert context.data_directories == frozenset({"interface"})
    assert context.protected_directories == frozenset({"meshes"})
    assert context.risky_paths == ("Fallout 4/Data",)
    assert context.glossary_sources == (glossary_source,)
    assert context.glossary_sources[0].consumers == frozenset({"rag"})


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("plugin_extensions", {1}),
        ("string_table_extensions", [None]),
        ("data_directories", [Path("interface")]),
        ("protected_directories", [object()]),
        ("risky_paths", [Path("Fallout 4/Data")]),
        ("glossary_sources", ["not-a-glossary-source"]),
    ],
)
def test_game_context_rejects_invalid_collection_elements(field: str, invalid) -> None:
    base = game_context.load_game_profile("fallout4")

    with pytest.raises(ValueError, match=field):
        replace(base, **{field: invalid})


def test_glossary_source_rejects_invalid_consumer_elements() -> None:
    with pytest.raises(ValueError, match="consumers"):
        game_context.GlossarySource(
            relative_path=Path("glossary/test.md"),
            format="markdown",
            consumers={1},
            recommended=True,
        )


def test_resource_model_rebuilds_extension_groups_and_rejects_wrong_elements() -> None:
    extensions = [".esp"]
    source_group = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions=extensions,
        capability="plugin_text",
    )
    model = game_context.ResourceModel(
        extension_groups=[source_group],
        containers={"interface": "interface"},
        trait_level_caps={},
    )

    extensions.append(".esm")

    assert model.extension_groups == (
        game_context.ResourceExtensionGroup(
            name="plugin",
            category="plugin",
            extensions=frozenset({".esp"}),
            capability="plugin_text",
        ),
    )
    assert model.extension_groups[0] is not source_group
    assert isinstance(model.extension_groups[0].extensions, frozenset)

    with pytest.raises(ValueError, match="ResourceExtensionGroup"):
        game_context.ResourceModel(
            extension_groups=[object()],
            containers={},
            trait_level_caps={},
        )


def test_game_context_rebuilds_resource_model_groups() -> None:
    base = game_context.load_game_profile("fallout4")
    source_group = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions={".esp"},
        capability="plugin_text",
    )
    source_model = game_context.ResourceModel(
        extension_groups=[source_group],
        containers={"interface": "interface"},
        trait_level_caps={},
    )

    context = replace(base, resource_model=source_model)

    assert context.resource_model is not source_model
    assert context.resource_model.extension_groups[0] is not source_model.extension_groups[0]


@pytest.mark.parametrize("construction", ["direct", "replace"])
def test_resource_model_defensively_copies_containers_and_trait_caps(
    construction: str,
) -> None:
    group = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions={".esp"},
        capability="plugin_text",
    )
    containers = {"interface": "interface"}
    plugin_caps = {"localized": "inventory_only"}
    trait_level_caps = {"plugin_text": plugin_caps}
    if construction == "direct":
        model = game_context.ResourceModel(
            extension_groups=[group],
            containers=containers,
            trait_level_caps=trait_level_caps,
        )
    else:
        base = game_context.ResourceModel(
            extension_groups=[group],
            containers={},
            trait_level_caps={},
        )
        model = replace(
            base,
            containers=containers,
            trait_level_caps=trait_level_caps,
        )

    containers["scripts"] = "papyrus"
    plugin_caps["localized"] = "stable"
    plugin_caps["light"] = "read_only"
    trait_level_caps["missing"] = {"light": "read_only"}

    assert model.containers == {"interface": "interface"}
    assert model.trait_level_caps == {
        "plugin_text": {"localized": "inventory_only"}
    }
    with pytest.raises(TypeError):
        model.containers["scripts"] = "papyrus"  # type: ignore[index]
    with pytest.raises(TypeError):
        model.trait_level_caps["plugin_text"]["light"] = "read_only"  # type: ignore[index]


@pytest.mark.parametrize(
    "containers",
    [
        {"": "interface"},
        {"Interface": "interface"},
        {" interface": "interface"},
        {"interface": "interface", "INTERFACE": "interface"},
        {"interface": ""},
        {"interface": "Interface"},
        {"interface": " interface"},
        {"interface": "typo"},
        {1: "interface"},
        {"interface": 1},
    ],
)
def test_resource_model_rejects_invalid_containers(containers) -> None:
    group = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions={".esp"},
        capability="plugin_text",
    )

    with pytest.raises(ValueError, match="container"):
        game_context.ResourceModel(
            extension_groups=[group],
            containers=containers,
            trait_level_caps={},
        )


@pytest.mark.parametrize(
    "trait_level_caps",
    [
        {"": {"light": "read_only"}},
        {"Plugin_Text": {"light": "read_only"}},
        {" plugin_text": {"light": "read_only"}},
        {1: {"light": "read_only"}},
        {"plugin_text": {"": "read_only"}},
        {"plugin_text": {"Light": "read_only"}},
        {"plugin_text": {" light": "read_only"}},
        {"plugin_text": {1: "read_only"}},
        {"plugin_text": {"light": "Read_Only"}},
        {"plugin_text": {"light": " read_only"}},
        {"plugin_text": {"light": "preview"}},
        {"plugin_text": {"light": 1}},
    ],
)
def test_resource_model_rejects_invalid_trait_level_caps(trait_level_caps) -> None:
    group = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions={".esp"},
        capability="plugin_text",
    )

    with pytest.raises(ValueError, match="trait_level_caps|trait level cap"):
        game_context.ResourceModel(
            extension_groups=[group],
            containers={},
            trait_level_caps=trait_level_caps,
        )


def test_game_context_rejects_unknown_trait_capability_on_replace() -> None:
    base = game_context.load_game_profile("fallout4")
    model = game_context.ResourceModel(
        extension_groups=base.resource_model.extension_groups,
        containers=base.resource_model.containers,
        trait_level_caps={"missing": {"light": "read_only"}},
    )

    with pytest.raises(ValueError, match="unknown capability.*missing"):
        replace(base, resource_model=model)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"name": ""}, "name.*canonical"),
        ({"name": "Plugin"}, "name.*canonical"),
        ({"category": "audio"}, "category.*unknown"),
        ({"extensions": []}, "extensions.*non-empty"),
        ({"extensions": ["esp"]}, "extensions.*canonical"),
        ({"extensions": [".esp", ".ESP"]}, "extensions.*duplicate"),
        ({"capability": 1}, "capability.*string"),
        ({"capability": "Plugin_Text"}, "capability.*canonical"),
    ],
)
def test_resource_extension_group_rejects_invalid_direct_construction(
    overrides,
    message: str,
) -> None:
    values = {
        "name": "plugin",
        "category": "plugin",
        "extensions": [".esp"],
        "capability": "plugin_text",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        game_context.ResourceExtensionGroup(**values)


@pytest.mark.parametrize("duplicate", ["name", "extension"])
def test_resource_model_rejects_cross_group_duplicates(duplicate: str) -> None:
    first = game_context.ResourceExtensionGroup(
        name="plugin",
        category="plugin",
        extensions=[".esp"],
        capability="plugin_text",
    )
    second = game_context.ResourceExtensionGroup(
        name="plugin" if duplicate == "name" else "plugin.other",
        category="plugin",
        extensions=[".esm" if duplicate == "name" else ".esp"],
        capability="plugin_text",
    )

    with pytest.raises(ValueError, match=f"duplicate.*{duplicate}"):
        game_context.ResourceModel(
            extension_groups=[first, second],
            containers={},
            trait_level_caps={},
        )


@pytest.mark.parametrize("construction", ["direct", "replace"])
def test_game_context_rejects_unknown_group_capability_without_loader(
    construction: str,
) -> None:
    base = game_context.load_game_profile("fallout4")
    model = game_context.ResourceModel(
        extension_groups=[
            game_context.ResourceExtensionGroup(
                name="plugin",
                category="plugin",
                extensions=[".esp"],
                capability="missing",
            )
        ],
        containers={},
        trait_level_caps={},
    )

    with pytest.raises(ValueError, match="resource group.*unknown capability.*missing"):
        if construction == "direct":
            values = {field.name: getattr(base, field.name) for field in fields(base)}
            values["resource_model"] = model
            game_context.GameContext(**values)
        else:
            replace(base, resource_model=model)
