from io import BytesIO
import os
from pathlib import Path
import shutil
import stat
import subprocess
import zipfile

import pytest

from tools import update_project as updater


def installation(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    (root / "机器人管理.cmd").write_text("old launcher", encoding="utf-8")
    (root / "qq_bot").mkdir()
    (root / "qq_bot/run.py").write_text("old bot", encoding="utf-8")
    return root


def archive(extra=None):
    files = {
        "机器人管理.cmd": b"new launcher",
        "qq_bot/run.py": b"new bot",
        "scripts/windows/update-project.cmd": b"launcher",
        "tools/update_project.py": b"updater",
        "tools/select_python.cmd": b"select python",
    }
    files.update(extra or {})
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        for name, content in files.items():
            package.writestr(name if name.startswith("/") else "qq_bot-main/" + name, content)
    return output.getvalue()


def test_zip_update_preserves_private_state_and_custom_poster_with_code_backup(tmp_path):
    root = installation(tmp_path)
    private = {
        ".env": b"PRIVATE_KEY=local",
        "secrets/api-key.txt": b"private credential",
        "data/state.db": b"private database",
        "docs/DSH人设.md": b"private persona",
        ".tools/browser-profile/cookies": b"login state",
        "昵称绑定.txt": b"private nicknames",
        "switch_presence.json": b"private settings",
        "assets/instruction.png": b"custom poster",
        "qq_bot/cordis.patch.yml": b"private local profile",
    }
    for name, content in private.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    count, backup = updater.install_archive(root, archive({
        **{name: b"must never replace private data" for name in private},
        "README.md": b"new documentation",
    }))
    assert count == 6
    assert (root / "qq_bot/run.py").read_bytes() == b"new bot"
    assert (backup / "qq_bot/run.py").read_bytes() == b"old bot"
    assert (backup / "机器人管理.cmd").read_bytes() == b"old launcher"
    assert all((root / name).read_bytes() == content for name, content in private.items())
    assert not (root / ".git").exists()
    assert updater.install_archive(root, archive({"README.md": b"new documentation"})) == (0, None)


@pytest.mark.parametrize("path", ["../../escape.py", "/absolute.py", "tools\\..\\escape.py", "tools/C:escape.py"])
def test_entire_package_is_validated_before_any_code_changes(tmp_path, path):
    root = installation(tmp_path)
    with pytest.raises(updater.UpdateError, match="路径"):
        updater.install_archive(root, archive({path: b"bad"}))
    assert (root / "qq_bot/run.py").read_text() == "old bot"
    assert not (root / "data").exists()


def test_symbolic_link_in_download_is_rejected_before_installing(tmp_path):
    root = installation(tmp_path)
    output = BytesIO(archive())
    with zipfile.ZipFile(output, "a") as package:
        info = zipfile.ZipInfo("qq_bot-main/tools/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, "../../outside")
    with pytest.raises(updater.UpdateError, match="符号链接"):
        updater.install_archive(root, output.getvalue())
    assert (root / "qq_bot/run.py").read_text() == "old bot"


def test_destination_link_cannot_write_outside_project(tmp_path):
    root = installation(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "tools").symlink_to(outside, target_is_directory=True)
    except OSError:
        # NTFS junctions do not need the symbolic-link privilege on Windows.
        if os.name != "nt":
            pytest.skip("Symbolic link creation is unavailable")
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(root / "tools"), str(outside)],
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("Junction creation is unavailable")
    with pytest.raises(updater.UpdateError, match="目录外|符号链接"):
        updater.install_archive(root, archive())
    assert list(outside.iterdir()) == []
    assert (root / "qq_bot/run.py").read_text() == "old bot"


def test_incomplete_or_oversized_package_does_not_touch_code(tmp_path, monkeypatch):
    root = installation(tmp_path)
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("qq_bot-main/README.md", "not a full project")
    with pytest.raises(updater.UpdateError, match="必要文件"):
        updater.install_archive(root, output.getvalue())
    monkeypatch.setattr(updater, "MAX_EXTRACTED_BYTES", 1)
    with pytest.raises(updater.UpdateError, match="过大"):
        updater.install_archive(root, archive())
    assert (root / "qq_bot/run.py").read_text() == "old bot"


def test_copy_failure_rolls_back_changed_and_new_files(tmp_path, monkeypatch):
    root = installation(tmp_path)
    real_replace = updater.os.replace
    def replace(source, destination):
        if destination.name == "fail.py":
            raise PermissionError("locked by another process")
        real_replace(source, destination)
    monkeypatch.setattr(updater.os, "replace", replace)
    with pytest.raises(updater.UpdateError, match="已恢复"):
        updater.install_archive(root, archive({"qq_bot/fail.py": b"locked"}))
    assert (root / "机器人管理.cmd").read_text() == "old launcher"
    assert (root / "qq_bot/run.py").read_text() == "old bot"
    assert not (root / "tools/update_project.py").exists()
    assert not list(root.rglob(".update-*"))


def test_git_mode_uses_explicit_correct_repository_instead_of_configured_upstream(tmp_path, monkeypatch):
    root = installation(tmp_path)
    calls = []
    def git(directory, *args):
        calls.append(args)
        if args[0] == "rev-parse":
            return str(root)
        if args[0] == "symbolic-ref":
            return "main"
        return ""
    monkeypatch.setattr(updater, "run_git", git)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "git")
    updater.update_git(root)
    assert ("fetch", "--no-tags", "https://github.com/KurolMato/qq_bot.git", "refs/heads/main") in calls
    assert calls[-1] == ("merge", "--ff-only", "FETCH_HEAD")
    assert not any(args[0] in {"pull", "reset", "checkout", "clean"} for args in calls)


@pytest.mark.parametrize("problem", ["dirty", "branch", "different-root"])
def test_git_preflight_failure_never_fetches_or_modifies_code(tmp_path, monkeypatch, problem):
    root = installation(tmp_path)
    calls = []
    def git(directory, *args):
        calls.append(args)
        if args[0] == "rev-parse":
            return str(root.parent if problem == "different-root" else root)
        if args[0] == "status":
            return " M qq_bot/run.py" if problem == "dirty" else ""
        if args[0] == "symbolic-ref":
            return "feature" if problem == "branch" else "main"
        pytest.fail("mutation after failed preflight")
    monkeypatch.setattr(updater, "run_git", git)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "git")
    with pytest.raises(updater.UpdateError):
        updater.update_git(root)
    assert not any(args[0] in {"fetch", "merge"} for args in calls)


def test_download_url_is_the_correct_repository_and_download_size_is_bounded(monkeypatch):
    seen = []
    def open_url(request, timeout):
        seen.append((request.full_url, timeout))
        return BytesIO(b"large")
    monkeypatch.setattr(updater, "urlopen", open_url)
    monkeypatch.setattr(updater, "MAX_ARCHIVE_BYTES", 2)
    with pytest.raises(updater.UpdateError, match="过大"):
        updater.download_archive()
    assert seen == [("https://codeload.github.com/KurolMato/qq_bot/zip/refs/heads/main", 60)]


@pytest.mark.skipif(shutil.which("git") is None, reason="Git required")
def test_real_git_fast_forward_ignores_wrong_origin_and_preserves_ignored_state(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    def git(root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Updater test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("first")
    (source / ".gitignore").write_text(".env\ndata/\nsecrets/\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", str(source), str(target)], check=True, capture_output=True)
    git(target, "remote", "set-url", "origin", str(tmp_path / "wrong-repository.git"))
    (target / ".env").write_text("private credentials")
    (target / "data").mkdir()
    (target / "data/state.db").write_bytes(b"private database")
    (source / "README.md").write_text("second")
    git(source, "add", ".")
    git(source, "commit", "-m", "update")
    monkeypatch.setattr(updater, "REPOSITORY_URL", str(source))
    updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert (target / "README.md").read_text() == "second"
    assert (target / ".env").read_text() == "private credentials"
    assert (target / "data/state.db").read_bytes() == b"private database"
    # A later local commit must never be reset or merged over to force an update.
    git(target, "config", "user.name", "Updater test")
    git(target, "config", "user.email", "test@example.invalid")
    (target / "README.md").write_text("local changes")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "local")
    before = git(target, "rev-parse", "HEAD")
    (source / "README.md").write_text("third")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "next update")
    with pytest.raises(updater.UpdateError, match="无法快进"):
        updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == before
    assert (target / "README.md").read_text() == "local changes"
