from __future__ import annotations

import random
from pathlib import Path

from qq_bot.dsh_media import (
    pick_sticker,
    resolve_sticker,
    sticker_categories,
    sticker_hint,
    split_save_url_markers,
    is_safe_image_url,
    safe_category,
    save_sticker,
    sniff_image_mime,
    split_save_markers,
    split_sticker_markers,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_split_save_markers() -> None:
    cleaned, categories = split_save_markers("好呀 [[保存表情:无语]] 收下了")
    assert cleaned == "好呀 收下了"
    assert categories == ["无语"]


def test_save_sticker_writes_and_dedupes(tmp_path: Path) -> None:
    saved = save_sticker(tmp_path, "无语", PNG)
    assert saved is not None and saved.exists()
    assert saved.parent.name == "无语"
    assert save_sticker(tmp_path, "无语", PNG) is None


def test_save_sticker_rejects_bad_category_and_data(tmp_path: Path) -> None:
    assert save_sticker(tmp_path, "../escape", PNG) is None
    assert save_sticker(tmp_path, "..", PNG) is None
    assert save_sticker(tmp_path, "", PNG) is None
    assert save_sticker(tmp_path, "无语", b"<html>") is None
    assert not (tmp_path.parent / "escape").exists()


def test_safe_category_normalises_brackets() -> None:
    assert safe_category("【无语】") == "无语"
    assert safe_category("a/b") is None
    assert safe_category("这个分类名字太长了超过十二个字") is None


def test_split_markers_returns_clean_text_and_category() -> None:
    cleaned, categories = split_sticker_markers("行吧 [[表情:无语]] 这次算你赢")
    assert cleaned == "行吧 这次算你赢"
    assert categories == ["无语"]


def test_split_markers_keeps_only_first_category() -> None:
    cleaned, categories = split_sticker_markers("[[sticker:开心]][[表情:生气]]")
    assert cleaned == ""
    assert categories == ["开心"]


def test_split_markers_without_marker_is_unchanged() -> None:
    cleaned, categories = split_sticker_markers("正常的一句话")
    assert cleaned == "正常的一句话"
    assert categories == []


def test_pick_sticker_returns_a_file_from_the_category(tmp_path: Path) -> None:
    folder = tmp_path / "开心"
    folder.mkdir()
    (folder / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (folder / "b.jpg").write_bytes(b"\xff\xd8\xfffake")
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    picked = pick_sticker(tmp_path, "开心", random.Random(1))
    assert picked is not None
    assert picked.name in {"a.png", "b.jpg"}


def test_pick_sticker_rejects_traversal_and_unknown_category(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (tmp_path / "..x").mkdir()
    assert pick_sticker(tmp_path, "..") is None
    assert pick_sticker(tmp_path, "../") is None
    assert pick_sticker(tmp_path, "不存在") is None


def test_pick_sticker_skips_oversized_files(tmp_path: Path) -> None:
    folder = tmp_path / "大图"
    folder.mkdir()
    (folder / "huge.png").write_bytes(b"\x89PNG" + b"0" * (5 * 1024 * 1024))
    assert pick_sticker(tmp_path, "大图") is None


def test_resolve_sticker_exact_file(tmp_path: Path) -> None:
    folder = tmp_path / "无语"
    folder.mkdir()
    target = folder / "01.png"
    target.write_bytes(PNG)
    assert resolve_sticker(tmp_path, "无语/01.png") == target


def test_resolve_sticker_rejects_escape_and_missing(tmp_path: Path) -> None:
    (tmp_path / "无语").mkdir()
    (tmp_path / "无语" / "01.png").write_bytes(PNG)
    assert resolve_sticker(tmp_path, "../outside.png") is None
    assert resolve_sticker(tmp_path, "无语/../01.png") is None
    assert resolve_sticker(tmp_path, "无语/不存在.png") is None


def test_resolve_sticker_falls_back_to_category(tmp_path: Path) -> None:
    folder = tmp_path / "开心"
    folder.mkdir()
    (folder / "a.png").write_bytes(PNG)
    picked = resolve_sticker(tmp_path, "开心")
    assert picked is not None and picked.parent.name == "开心"


def test_sticker_categories_and_hint(tmp_path: Path) -> None:
    (tmp_path / "无语").mkdir()
    (tmp_path / "无语" / "a.png").write_bytes(PNG)
    (tmp_path / "摸鱼").mkdir()  # empty folders are not offered to the model
    (tmp_path / "README.txt").write_text("x", encoding="utf-8")
    assert sticker_categories(tmp_path) == ["无语"]
    hint = sticker_hint(tmp_path)
    assert "无语" in hint and "摸鱼" not in hint and "[[表情:分类]]" in hint


def test_sticker_hint_empty_without_categories(tmp_path: Path) -> None:
    assert sticker_hint(tmp_path) == ""


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xffrest") == "image/jpeg"
    assert sniff_image_mime(b"GIF89arest") == "image/gif"
    assert sniff_image_mime(b"RIFF1234WEBPrest") == "image/webp"
    assert sniff_image_mime(b"<html>") is None


def test_split_markers_accepts_fullwidth_brackets() -> None:
    cleaned, categories = split_sticker_markers("［［表情：开饭］］ 好耶")
    assert cleaned == "好耶"
    assert categories == ["开饭"]


def test_malformed_marker_is_still_stripped() -> None:
    cleaned, categories = split_sticker_markers("[[表情 开饭]] 走")
    assert "[[表情" not in cleaned
    assert categories == []


def test_normal_brackets_are_untouched() -> None:
    cleaned, categories = split_sticker_markers("看这个 [备注] 就好了")
    assert cleaned == "看这个 [备注] 就好了"
    assert categories == []

def test_save_url_marker() -> None:
    cleaned, items = split_save_url_markers("哈 [[收图:开饭|https://example.com/a.png]] 收好")
    assert cleaned == "哈 收好"
    assert items == [("开饭", "https://example.com/a.png")]


def test_save_url_marker_limits_two() -> None:
    text = "[[收图:A|https://a/1.png]][[收图:B|https://b/2.png]][[收图:C|https://c/3.png]]"
    _cleaned, items = split_save_url_markers(text)
    assert len(items) == 2


def test_is_safe_image_url() -> None:
    assert is_safe_image_url("https://example.com/a.png") is True
    assert is_safe_image_url("http://127.0.0.1/a.png") is False
    assert is_safe_image_url("http://192.168.1.2/a.png") is False
    assert is_safe_image_url("file:///C:/a.png") is False
    assert is_safe_image_url("not a url") is False