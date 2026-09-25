from pathlib import Path

from qq_bot import nickname_batch
from qq_bot.switch_registry import SwitchRegistry, SwitchSubscription


def _switch_item(group_id: str, nickname: str | None) -> SwitchSubscription:
    return SwitchSubscription(
        friend_code="1234-5678-9012",
        nsa_id="nsa-id",
        ns_name="示例玩家",
        avatar_url=None,
        qq_user_id="100",
        group_id=group_id,
        status="active",
        current_game=None,
        initialised=True,
        nickname=nickname,
    )


def test_export_deduplicates_groups_and_import_updates_all_groups(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(nickname_batch, "DATA_DIR", data_dir)
    registry = SwitchRegistry(data_dir / "switch_registry.db")
    registry.add(_switch_item("group-1", "旧昵称"))
    registry.add(_switch_item("group-2", None))

    bindings = tmp_path / "昵称绑定.txt"
    count, backup = nickname_batch.export_bindings(bindings)
    content = bindings.read_text(encoding="utf-8-sig")

    assert count == 1
    assert backup is None
    assert content.count("switch|1234-5678-9012|示例玩家|旧昵称") == 1

    bindings.write_text(
        "平台|ID|示例玩家|昵称\n"
        "switch|1234-5678-9012|示例玩家|新昵称\n",
        encoding="utf-8-sig",
    )
    changed, unchanged, missing, backup_dir = nickname_batch.import_bindings(bindings)

    assert changed == 2
    assert unchanged == 0
    assert missing == []
    assert backup_dir is not None
    assert (backup_dir / "switch_registry.db").exists()
    assert {item.nickname for item in registry.list_all()} == {"新昵称"}


def test_blank_nickname_is_ignored_and_clear_marker_removes_it(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(nickname_batch, "DATA_DIR", data_dir)
    registry = SwitchRegistry(data_dir / "switch_registry.db")
    registry.add(_switch_item("group-1", "保留我"))
    bindings = tmp_path / "昵称绑定.txt"

    bindings.write_text(
        "平台|ID|示例玩家|昵称\n"
        "switch|1234-5678-9012|示例玩家|\n",
        encoding="utf-8-sig",
    )
    changed, unchanged, missing, backup_dir = nickname_batch.import_bindings(bindings)
    assert (changed, unchanged, missing, backup_dir) == (0, 0, [], None)
    assert registry.list_all()[0].nickname == "保留我"

    bindings.write_text(
        "平台|ID|示例玩家|昵称\n"
        "switch|1234-5678-9012|示例玩家|[清空]\n",
        encoding="utf-8-sig",
    )
    changed, unchanged, missing, backup_dir = nickname_batch.import_bindings(bindings)
    assert changed == 1
    assert unchanged == 0
    assert missing == []
    assert backup_dir is not None
    assert registry.list_all()[0].nickname is None


def test_import_accepts_nickname_appended_after_existing_trailing_separator(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(nickname_batch, "DATA_DIR", data_dir)
    registry = SwitchRegistry(data_dir / "switch_registry.db")
    registry.add(_switch_item("group-1", None))
    bindings = tmp_path / "昵称绑定.txt"
    bindings.write_text(
        "平台|ID|示例玩家|昵称\n"
        "switch|1234-5678-9012|示例玩家||追加昵称\n",
        encoding="utf-8-sig",
    )

    changed, unchanged, missing, _backup_dir = nickname_batch.import_bindings(bindings)

    assert changed == 1
    assert unchanged == 0
    assert missing == []
    assert registry.list_all()[0].nickname == "追加昵称"
