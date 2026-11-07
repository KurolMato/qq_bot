"""Verified title aliases for cross-platform popularity; no fuzzy translation."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
import unicodedata


# Canonical display title first. Add verified translations here, not fuzzy matches.
TITLE_ALIASES = (("影之刃零", "Phantom Blade Zero", "影之刃 零"),)


def title_key(name: str) -> str:
    name = name.translate(str.maketrans("", "", "™®©"))
    name = unicodedata.normalize("NFKC", name).casefold()
    return "".join(c for c in name if not c.isspace() and c not in "™®©")


def title_identities(path: Path | None = None, group_id: str = "") -> dict[str, str]:
    """Join explicit aliases and names observed under the same scoped game ID.

    Historical names are read across periods so an old English observation can
    match this week's Chinese title. IDs never match directly across platforms.
    Missing/old databases fall back to the explicit aliases without writes.
    """
    groups = []
    if path is not None and Path(path).is_file():
        try:
            with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                rows = db.execute(
                    "SELECT DISTINCT platform, game_key, game_name FROM game_time_buckets "
                    "WHERE group_id = ? AND game_key IS NOT NULL AND game_name IS NOT NULL "
                    "UNION SELECT platform, game_key, game_name FROM game_time_accounts "
                    "WHERE group_id = ? AND game_key IS NOT NULL AND game_name IS NOT NULL",
                    (str(group_id), str(group_id)),
                ).fetchall()
            observed = defaultdict(set)
            for platform, game_id, name in rows:
                if str(game_id).strip() and str(name).strip():
                    observed[(platform, game_id)].add(name)
            groups.extend(observed.values())
        except sqlite3.Error:
            pass
    groups.extend(TITLE_ALIASES)
    parent = {}
    names = defaultdict(set)

    def root(key):
        parent.setdefault(key, key)
        while key != parent[key]:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for group in groups:
        keys = []
        for name in group:
            key = title_key(name)
            if key:
                keys.append(key)
                names[key].add(name)
        for key in keys[1:]:
            parent[root(key)] = root(keys[0])
        if keys:
            root(keys[0])
    components = defaultdict(set)
    for key in parent:
        components[root(key)].update(names[key])
    preferred = {root(title_key(group[0])): group[0] for group in TITLE_ALIASES}
    labels = {key: preferred.get(key) or min(values, key=lambda name: (
        not any("\u3400" <= c <= "\u9fff" for c in name), title_key(name), name,
    )) for key, values in components.items()}
    return {key: labels[root(key)] for key in parent}
