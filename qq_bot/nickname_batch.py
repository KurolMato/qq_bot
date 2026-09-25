from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .nicknames import normalize_nickname


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_BINDINGS_PATH = PROJECT_ROOT / "昵称绑定.txt"
CLEAR_MARKER = "[清空]"


@dataclass(frozen=True)
class PlatformSpec:
    label: str
    db_name: str
    table: str
    id_column: str
    name_column: str
    case_insensitive: bool = False


PLATFORMS = {
    "switch": PlatformSpec(
        "switch", "switch_registry.db", "switch_subscriptions", "friend_code", "ns_name"
    ),
    "psn": PlatformSpec(
        "psn", "ps5_registry.db", "ps5_subscriptions", "online_id", "online_id", True
    ),
    "steam": PlatformSpec(
        "steam", "steam_registry.db", "steam_subscriptions", "steam_id", "display_name"
    ),
    "xbox": PlatformSpec(
        "xbox", "xbox_registry.db", "xbox_subscriptions", "gamertag", "gamertag", True
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _safe_cell(value: object | None) -> str:
    return str(value or "").replace("|", "／").replace("\r", " ").replace("\n", " ").strip()


def _backup_existing_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.before-export-{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def export_bindings(path: Path) -> tuple[int, Path | None]:
    rows: list[tuple[str, str, str, str]] = []
    for platform, spec in PLATFORMS.items():
        db_path = DATA_DIR / spec.db_name
        if not db_path.exists():
            continue
        with _connect(db_path) as db:
            values = db.execute(
                f"SELECT {spec.id_column} AS identifier, {spec.name_column} AS account_name, "
                f"nickname FROM {spec.table} ORDER BY created_at"
            ).fetchall()

        grouped: dict[str, list[sqlite3.Row]] = {}
        display_ids: dict[str, str] = {}
        for item in values:
            identifier = str(item["identifier"] or "").strip()
            if not identifier:
                continue
            key = identifier.casefold() if spec.case_insensitive else identifier
            grouped.setdefault(key, []).append(item)
            display_ids.setdefault(key, identifier)

        for key, items in grouped.items():
            nicknames = {
                str(item["nickname"]).strip()
                for item in items
                if item["nickname"] and str(item["nickname"]).strip()
            }
            # Conflicting per-group nicknames are left blank so the next import
            # deliberately unifies them instead of silently choosing one.
            nickname = next(iter(nicknames)) if len(nicknames) == 1 else ""
            account_name = next(
                (_safe_cell(item["account_name"]) for item in items if item["account_name"]), ""
            )
            rows.append((platform, display_ids[key], account_name, nickname))

    backup = _backup_existing_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="\n") as output:
        output.write("# 昵称批量绑定表：只修改最后一列“昵称”，不要修改平台和ID。\n")
        output.write("# 空昵称=保持原值；填写 [清空] 可删除昵称；同一ID会同步到所有已登记群。\n")
        output.write("平台|ID|当前账号名（仅供参考）|昵称\n")
        for platform, identifier, account_name, nickname in rows:
            output.write(
                f"{platform}|{_safe_cell(identifier)}|{account_name}|{_safe_cell(nickname)}\n"
            )
    return len(rows), backup


def _parse_bindings(path: Path) -> list[tuple[int, str, str, str]]:
    if not path.exists():
        raise ValueError(f"找不到绑定表：{path}")
    parsed: list[tuple[int, str, str, str]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("平台|"):
            continue
        parts = [part.strip() for part in line.split("|")]
        # A common editing pattern is to append "|昵称" after the template's
        # existing trailing separator, producing ...|账号名||昵称. Accept that
        # harmless extra empty column instead of rejecting the whole import.
        if len(parts) == 5 and not parts[3] and parts[4]:
            parts = parts[:3] + [parts[4]]
        if len(parts) != 4:
            raise ValueError(f"第 {line_number} 行应有4列，并用英文竖线 | 分隔。")
        platform, identifier, _account_name, nickname = parts
        platform = platform.casefold()
        if platform not in PLATFORMS:
            raise ValueError(f"第 {line_number} 行平台无效：{platform}")
        if not identifier:
            raise ValueError(f"第 {line_number} 行 ID 不能为空。")
        if nickname and nickname != CLEAR_MARKER:
            try:
                nickname = normalize_nickname(nickname)
            except ValueError as exc:
                raise ValueError(f"第 {line_number} 行昵称无效：{exc}") from exc
        parsed.append((line_number, platform, identifier, nickname))
    return parsed


def _backup_database(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / source.name
    with _connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)
    return destination


def import_bindings(path: Path) -> tuple[int, int, list[str], Path | None]:
    entries = _parse_bindings(path)
    actionable = [entry for entry in entries if entry[3]]
    if not actionable:
        return 0, 0, [], None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = DATA_DIR / "nickname_backups" / stamp
    changed = 0
    unchanged = 0
    missing: list[str] = []
    backed_up: set[str] = set()

    for _line_number, platform, identifier, nickname in actionable:
        spec = PLATFORMS[platform]
        db_path = DATA_DIR / spec.db_name
        if not db_path.exists():
            missing.append(f"{platform}:{identifier}（数据库不存在）")
            continue
        if spec.db_name not in backed_up:
            _backup_database(db_path, backup_dir)
            backed_up.add(spec.db_name)

        value = None if nickname == CLEAR_MARKER else nickname
        comparison = f"lower({spec.id_column}) = lower(?)" if spec.case_insensitive else f"{spec.id_column} = ?"
        with _connect(db_path) as db:
            existing = db.execute(
                f"SELECT nickname FROM {spec.table} WHERE {comparison}", (identifier,)
            ).fetchall()
            if not existing:
                missing.append(f"{platform}:{identifier}")
                continue
            result = db.execute(
                f"UPDATE {spec.table} SET nickname = ?, updated_at = ? "
                f"WHERE {comparison} AND COALESCE(nickname, '') <> COALESCE(?, '')",
                (value, utc_now(), identifier, value),
            )
            changed += result.rowcount
            unchanged += len(existing) - result.rowcount

    return changed, unchanged, missing, backup_dir if backed_up else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量导出或导入三个游戏平台的昵称。")
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("--file", type=Path, default=DEFAULT_BINDINGS_PATH)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.action == "export":
            count, backup = export_bindings(args.file)
            print(f"已导出 {count} 个账号：{args.file}")
            if backup:
                print(f"旧绑定表已备份：{backup}")
            print("请只修改最后一列“昵称”，保存后在机器人管理器中选择“导入昵称表”。")
            return 0

        changed, unchanged, missing, backup_dir = import_bindings(args.file)
        print(f"导入完成：更新 {changed} 条登记，保持不变 {unchanged} 条。")
        if backup_dir:
            print(f"导入前数据库备份：{backup_dir}")
        if missing:
            print("以下 ID 未在当前登记中找到：")
            for item in missing:
                print(f"  - {item}")
        return 2 if missing else 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
