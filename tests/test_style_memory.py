from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from qq_bot.style_memory import (
    append_sample,
    load_samples,
    merge_points,
    parse_style_points,
    style_hint,
    scopes_in_samples,
)


def test_append_and_load_samples(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    append_sample(path, "group:1", "  今天  好困  ", now=100)
    append_sample(path, "group:2", "别的群", now=101)
    append_sample(path, "group:1", "先去睡了", now=102)
    assert load_samples(path, "group:1") == "今天 好困\n先去睡了"
    assert load_samples(path, "group:1", since=101) == "先去睡了"
    assert load_samples(path, "group:2") == "别的群"


def test_load_samples_respects_char_limit(tmp_path: Path) -> None:
    path = tmp_path / "samples.jsonl"
    for index in range(5):
        append_sample(path, "g", "x" * 10, now=index)
    text = load_samples(path, "g", limit_chars=25)
    assert text.count("x") == 20


def test_parse_points_from_json_or_lines() -> None:
    assert parse_style_points('好的 ["句子短", "爱用草"] 结束') == ["句子短", "爱用草"]
    assert parse_style_points("- 句子短\n- 不爱用标点") == ["句子短", "不爱用标点"]
    assert parse_style_points("没有要点") == []


def test_merge_points_caps_and_dedupes() -> None:
    merged = merge_points(["a", "b"], ["b", "c", "d"], cap=3)
    assert merged == ["b", "c", "d"]


def test_style_hint_empty_and_filled() -> None:
    assert style_hint({"scopes": {}}, "g") == ""
    hint = style_hint({"scopes": {"g": {"points": ["句子短", "爱用草"]}}}, "g")
    assert "句子短" in hint and "自然贴合" in hint


def test_damaged_records_do_not_block_sampling(tmp_path):
    path = tmp_path / "samples.jsonl"
    append_sample(path, "g", "正常中文", now=1)
    with path.open("ab") as stream:
        stream.write(b'\x8a\n[]\nnull\n{"ts": "bad"}\n')
    append_sample(path, "g", "仍然可用", now=2)
    assert load_samples(path, "g") == "正常中文\n仍然可用"
    assert scopes_in_samples(path) == {"g"}


def test_concurrent_compaction_preserves_other_scopes(tmp_path):
    path = tmp_path / "samples.jsonl"
    def write(scope):
        for i in range(10):
            append_sample(path, scope, f"中文{i}", max_samples=5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, ["a", "b", "c", "d"]))
    for scope in ["a", "b", "c", "d"]:
        assert load_samples(path, scope).splitlines() == [f"中文{i}" for i in range(5, 10)]
    assert all(isinstance(json.loads(line), dict) for line in path.read_text(encoding="utf-8").splitlines())


def test_compaction_replace_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "samples.jsonl"
    append_sample(path, "g", "first", max_samples=1)
    append_sample(path, "g", "second", max_samples=1)
    def fail(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(PermissionError):
        append_sample(path, "g", "third", max_samples=1)
    assert load_samples(path, "g") == "first\nsecond\nthird"
    assert not list(tmp_path.glob("*.tmp"))
