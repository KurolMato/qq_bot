from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qq_bot.dsh_client import (
    DshClient,
    DshSettings,
    ImagePart,
    SessionStore,
    clean_answer,
    split_bubbles,
    split_message,
    trim_answer,
)

FAKE_SERVER = Path(__file__).with_name("fake_acp_server.py")


@pytest.fixture()
def settings(tmp_path: Path) -> DshSettings:
    return DshSettings(
        command=[sys.executable, str(FAKE_SERVER)],
        profile="qq",
        home=tmp_path,
        workdir=tmp_path,
        timeout=10.0,
        idle_seconds=0.0,
    )


@pytest.fixture()
def client(settings: DshSettings):
    instance = DshClient(settings)
    yield instance
    instance.stop()


def test_prompt_returns_only_assistant_messages(client: DshClient) -> None:
    session_id = client.new_session()
    answer = client.prompt(session_id, "hello")
    assert answer.text == "FAKE:hello"
    assert "THOUGHT" not in answer.text
    assert answer.cancelled is False


def test_tool_calls_are_reported(client: DshClient) -> None:
    session_id = client.new_session()
    answer = client.prompt(session_id, "tool")
    assert answer.text == "READ-OK"
    assert "read data/qq-bot.log" in answer.tools


def test_permission_requests_are_rejected(client: DshClient) -> None:
    session_id = client.new_session()
    answer = client.prompt(session_id, "write")
    assert answer.text == "DENIED"


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_launch_policy_overrides_profile_and_is_cleaned_up(settings, mode):
    settings.permission_mode = mode
    instance = DshClient(settings)
    try:
        answer = instance.prompt("", "policy")
        assert answer.text == f"POLICY:{mode}:{mode}:{mode}"
        policy_dir = Path(instance._policy_dir.name)
        assert policy_dir.is_dir()
        # Workspace writes do not grant unrelated privilege escalation.
        assert instance.prompt(answer.session_id, "write").text == "DENIED"
    finally:
        instance.stop()
    assert not policy_dir.exists()


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_browser_plugin_only_mounts_in_writable_owner_process(settings, mode):
    settings.permission_mode = mode
    settings.browser_mcp = {"serverName": "browser", "transport": "stdio", "command": "mock-browser"}
    instance = DshClient(settings)
    try:
        assert instance.prompt("", "browser-policy").text == f"BROWSER:{int(mode == 'workspace-write')}"
    finally:
        instance.stop()


def test_prompt_sends_image_blocks(client: DshClient) -> None:
    answer = client.prompt("persisted-session", "看图", [ImagePart(b"\x89PNG\r\n\x1a\nrest", "image/png")])
    assert answer.text == "IMAGES:1"


def test_prompt_resumes_persisted_session(client: DshClient) -> None:
    answer = client.prompt("persisted-session", "hello")
    assert answer.session_id == "persisted-session"
    assert answer.text == "FAKE:hello"


def test_prompt_starts_new_session_when_resume_fails(client: DshClient) -> None:
    answer = client.prompt("gone-session", "hello")
    assert answer.session_id == "fake-session"
    assert answer.text == "FAKE:hello"


def test_timeout_cancels_the_turn(client: DshClient, settings: DshSettings) -> None:
    settings.timeout = 1.0
    session_id = client.new_session()
    answer = client.prompt(session_id, "slow")
    assert answer.stop_reason == "timeout"
    assert answer.cancelled is True
    assert answer.text == "PARTIAL"
    # An unacknowledged cancellation must not leak late text into another turn.
    assert not client.alive()
    assert answer.elapsed < 3.0


def test_idle_watchdog_does_not_stop_active_work(client, settings, monkeypatch):
    settings.idle_seconds = 1
    monkeypatch.setattr(client, "alive", lambda: True)
    monkeypatch.setattr(client, "stop", lambda: pytest.fail("active process stopped"))
    client._pending[1] = object()
    assert client.maybe_stop_idle() is False
    client._pending.clear()
    client._prompts["busy"] = object()
    assert client.maybe_stop_idle() is False
    client._prompts.clear()


def test_session_store_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    assert store.get("10001") == ""
    store.bind("10001", "session-new")
    assert store.get("10001") == "session-new"
    assert store.info("10001")["turns"] == 0

    store.record("10001", "session-new", 12.5)
    assert store.get("10001") == "session-new"
    assert store.info("10001")["turns"] == 1

    reloaded = SessionStore(path)
    assert reloaded.get("10001") == "session-new"
    reloaded.forget("10001")
    assert reloaded.get("10001") == ""
    assert SessionStore(path).get("10001") == ""


def test_split_message_keeps_short_text_intact() -> None:
    assert split_message("一句话") == ["一句话"]
    assert split_message("   ") == []


def test_split_message_limits_parts_and_marks_truncation() -> None:
    body = "\n\n".join("段落" * 60 for _ in range(12))
    parts = split_message(body, limit=300)
    assert len(parts) <= 3
    assert all(len(part) <= 340 for part in parts)
    assert "已截断" in parts[-1]


def test_split_bubbles_one_per_line() -> None:
    assert split_bubbles("第一句\n第二句\n\n第三句") == ["第一句", "第二句", "第三句"]


def test_split_bubbles_caps_parts_without_losing_text() -> None:
    bubbles = split_bubbles("一\n二\n三\n四\n五\n六", max_parts=4)
    assert len(bubbles) == 4
    assert bubbles[-1] == "四，五，六"


def test_split_bubbles_splits_chinese_commas() -> None:
    assert split_bubbles("然后重启，人家改不了，只读") == ["然后重启", "人家改不了", "只读"]


def test_split_bubbles_keeps_single_char_bubbles() -> None:
    assert split_bubbles("嗯，好") == ["嗯", "好"]


def test_split_bubbles_keeps_long_comma_sentence_whole() -> None:
    line = "这是一个比较长的句子，里面还有好几个逗号，但是整句超过阈值就保持一条"
    assert split_bubbles(line) == [line]


def test_split_bubbles_unlimited_when_max_parts_zero() -> None:
    text = "。".join(f"第{i}句" for i in range(10))
    assert len(split_bubbles(text, max_parts=0, comma_split_max_len=200)) == 10


def test_split_bubbles_hard_cap_stops_runaway_chain() -> None:
    text = "。".join(f"第{i}句" for i in range(40))
    bubbles = split_bubbles(text, max_parts=0, comma_split_max_len=500)
    assert len(bubbles) == 30
    assert bubbles[-1].startswith("第29句")


def test_split_bubbles_cuts_long_line() -> None:
    line = "这是很长的一句话" * 30
    bubbles = split_bubbles(line, max_parts=2, max_len=40)
    assert len(bubbles) == 2
    assert len(bubbles[0]) <= 40
    assert line.startswith(bubbles[0])


def test_trim_answer_keeps_short_replies() -> None:
    assert trim_answer("一句话结论。") == "一句话结论。"
    assert trim_answer("") == ""


def test_trim_answer_cuts_at_sentence_end() -> None:
    text = "今天日志里没有新的错误，机器人整体正常。" + "补充说明" * 80
    trimmed = trim_answer(text, 30)
    assert trimmed == "今天日志里没有新的错误，机器人整体正常。…"


def test_trim_answer_falls_back_to_hard_cut() -> None:
    trimmed = trim_answer("没有标点" * 50, 20)
    assert len(trimmed) == 21
    assert trimmed.endswith("…")


def test_clean_answer_removes_terminal_and_markdown_noise() -> None:
    raw = "\x1b[31m**结论**\x1b[0m\n```\ncode\n```\n## 小标题\n`inline`"
    cleaned = clean_answer(raw)
    assert "**" not in cleaned
    assert "```" not in cleaned
    assert "\x1b" not in cleaned
    assert "结论" in cleaned
    assert "小标题" in cleaned
    assert "inline" in cleaned


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QQ_DSH_HOME", str(tmp_path))
    monkeypatch.setenv("QQ_DSH_CLI", str(tmp_path / "bin.js"))
    monkeypatch.setenv("QQ_DSH_TIMEOUT", "90")
    settings = DshSettings.from_env(tmp_path)
    assert settings.command[1] == str(tmp_path / "bin.js")
    assert settings.timeout == 90.0
    assert settings.workdir == tmp_path


def test_owner_permission_mode_is_configurable(monkeypatch, tmp_path):
    monkeypatch.delenv("QQ_DSH_PERMISSION_MODE", raising=False)
    assert DshSettings.from_env(tmp_path).permission_mode == "workspace-write"
    monkeypatch.setenv("QQ_DSH_PERMISSION_MODE", "read-only")
    assert DshSettings.from_env(tmp_path).permission_mode == "read-only"
    monkeypatch.setenv("QQ_DSH_PERMISSION_MODE", "danger-full-access")
    with pytest.raises(ValueError, match="QQ_DSH_PERMISSION_MODE"):
        DshSettings.from_env(tmp_path)


def test_settings_ignore_harness_dsh_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The bot may itself run inside a harness session that exports DSH_HOME or
    # DSH_PROFILE; the bridge must stay on its own profile and home.
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "harness"))
    monkeypatch.setenv("DSH_PROFILE", "desktop")
    monkeypatch.delenv("QQ_DSH_HOME", raising=False)
    monkeypatch.delenv("QQ_DSH_PROFILE", raising=False)
    settings = DshSettings.from_env(tmp_path)
    assert settings.profile == "qq"
    assert settings.home.name == ".dsh-qq"
