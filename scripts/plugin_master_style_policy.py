"""Load the versioned official Full-master policy shared by plugin evidence stages."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


MASTER_STYLE_POLICY_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "plugin_master_styles.json"
)


def _conflict(message: str) -> ValueError:
    return ValueError(f"master_style_conflict: {message}")


@lru_cache(maxsize=1)
def _known_full_master_policy() -> dict[str, frozenset[str]]:
    try:
        payload = json.loads(MASTER_STYLE_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _conflict(
            f"known-full master policy is invalid: {MASTER_STYLE_POLICY_PATH}"
        ) from exc
    rows = payload.get("known_full_masters") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(rows, dict)
    ):
        raise _conflict("known-full master policy must use schema_version 1")
    policy: dict[str, frozenset[str]] = {}
    for game_id, names in rows.items():
        if not isinstance(game_id, str) or not isinstance(names, list) or not names:
            raise _conflict("known-full master policy contains an invalid game entry")
        normalized: set[str] = set()
        for name in names:
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or Path(name).suffix.casefold() not in {".esp", ".esm"}
                or name.casefold() in normalized
            ):
                raise _conflict(
                    f"known-full master policy contains an invalid master for {game_id}"
                )
            normalized.add(name.casefold())
        policy[game_id] = frozenset(normalized)
    return policy


def known_full_masters(game_id: str) -> frozenset[str]:
    policy = _known_full_master_policy()
    try:
        return policy[game_id]
    except KeyError as exc:
        raise _conflict(
            f"known-full master policy does not define game_id {game_id}"
        ) from exc
