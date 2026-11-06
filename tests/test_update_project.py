from io import BytesIO
import os
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
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


def release_files():
    return {
        "机器人管理.cmd": b"new launcher",
        "qq_bot/run.py": b"new bot",
        "scripts/windows/update-project.cmd": b"launcher",
        "tools/update_project.py": b"updater",
        "tools/select_python.cmd": b"select python",
    }


def archive(extra=None):
    files = release_files()
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
        "assets/instruction.local.png": b"private custom poster",
        "qq_bot/cordis.patch.yml": b"private local profile",
    }
    for name, content in private.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    (root / "assets/instruction.png").write_bytes(b"modified default poster")
    count, backup = updater.install_archive(root, archive({
        **{name: b"must never replace private data" for name in private},
        "assets/INSTRUCTION.LOCAL.PNG": b"must not replace the local override",
        "qq_bot/CORDIS.PATCH.YML": b"must not replace the local profile",
        "README.md": b"new documentation",
        "assets/instruction.png": b"official default poster",
    }))
    assert count == 7
    assert (root / "qq_bot/run.py").read_bytes() == b"new bot"
    assert (backup / "qq_bot/run.py").read_bytes() == b"old bot"
    assert (backup / "机器人管理.cmd").read_bytes() == b"old launcher"
    assert all((root / name).read_bytes() == content for name, content in private.items())
    assert (root / "assets/instruction.png").read_bytes() == b"official default poster"
    assert (backup / "assets/instruction.png").read_bytes() == b"modified default poster"
    assert not (root / ".git").exists()
    assert updater.install_archive(root, archive({
        "README.md": b"new documentation", "assets/instruction.png": b"official default poster",
    })) == (0, None)


def test_zip_installation_receives_bundled_default_poster(tmp_path):
    root = installation(tmp_path)
    image = Path(__file__).resolve().parents[1] / "assets/instruction.png"
    updater.install_archive(root, archive({"assets/instruction.png": image.read_bytes()}))
    assert (root / "assets/instruction.png").read_bytes() == image.read_bytes()
    assert not (root / "assets/instruction.local.png").exists()


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


def test_git_fetch_failure_never_changes_code(tmp_path, monkeypatch):
    root = installation(tmp_path)
    calls = []
    def git(directory, *args):
        calls.append(args)
        if args[0] == "rev-parse":
            return str(root)
        if args[0] == "fetch":
            raise updater.UpdateError("network unavailable")
        pytest.fail("mutation after failed fetch")
    monkeypatch.setattr(updater, "run_git", git)
    monkeypatch.setattr(updater.shutil, "which", lambda name: "git")
    with pytest.raises(updater.UpdateError, match="network unavailable"):
        updater.update_git(root)
    assert ("fetch", "--no-tags", "https://github.com/KurolMato/qq_bot.git", "refs/heads/main") in calls
    assert not any(args[0] in {"pull", "reset", "checkout", "clean"} for args in calls)
    assert (root / "qq_bot/run.py").read_text() == "old bot"
    assert not (root / "data").exists()


def test_git_parent_repository_is_not_modified(tmp_path, monkeypatch):
    root = installation(tmp_path)
    calls = []
    def git(directory, *args):
        calls.append(args)
        if args[0] == "rev-parse":
            return str(root.parent)
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


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.STDOUT).decode().strip()


def write_files(root, files):
    for name, content in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)


def init_git(root):
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Updater test")
    git(root, "config", "user.email", "test@example.invalid")


@pytest.fixture
def git_installation(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("Git required")
    source = tmp_path / "source"
    source.mkdir()
    init_git(source)
    write_files(source, {**release_files(), "README.md": b"first",
        ".gitignore": b".env\ndata/\nsecrets/\nassets/instruction.local.png\n",
        "qq_bot/obsolete.py": b"old module",
    })
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    target = tmp_path / "target"
    subprocess.run(["git", "clone", str(source), str(target)], check=True, capture_output=True)
    git(target, "remote", "set-url", "origin", str(tmp_path / "wrong-repository.git"))
    git(target, "config", "user.name", "Updater test")
    git(target, "config", "user.email", "test@example.invalid")
    (target / ".env").write_text("private credentials")
    (target / "data").mkdir()
    (target / "data/state.db").write_bytes(b"private database")
    (target / "assets").mkdir()
    (target / "assets/instruction.local.png").write_bytes(b"local custom poster")
    (source / "README.md").write_text("second")
    (source / "assets").mkdir()
    (source / "assets/instruction.png").write_bytes(b"bundled default poster")
    (source / "qq_bot/obsolete.py").unlink()
    git(source, "add", ".")
    git(source, "commit", "-m", "update")
    monkeypatch.setattr(updater, "REPOSITORY_URL", str(source))
    return source, target


def test_force_git_replaces_dirty_staged_and_divergent_code_but_keeps_local_state(git_installation):
    source, target = git_installation
    git(target, "checkout", "-b", "local-feature")
    (target / "README.md").write_text("local commit")
    (target / "qq_bot/local_only.py").write_text("local committed module")
    git(target, "add", ".")
    git(target, "commit", "-m", "local changes")
    old_head = git(target, "rev-parse", "HEAD")
    (target / "qq_bot/run.py").write_text("staged bot")
    git(target, "add", "qq_bot/run.py")
    (target / "qq_bot/run.py").write_text("unstaged bot")
    (target / "qq_bot/untracked-extra.py").write_text("local extra file")
    updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert (target / "README.md").read_text() == "second"
    assert (target / ".env").read_text() == "private credentials"
    assert (target / "data/state.db").read_bytes() == b"private database"
    assert (target / "assets/instruction.png").read_bytes() == b"bundled default poster"
    assert (target / "assets/instruction.local.png").read_bytes() == b"local custom poster"
    assert (target / "qq_bot/untracked-extra.py").read_text() == "local extra file"
    assert not (target / "qq_bot/local_only.py").exists()
    assert not (target / "qq_bot/obsolete.py").exists()
    assert git(target, "diff", "--name-only") == ""
    assert git(target, "diff", "--cached", "--name-only") == ""
    assert git(target, "symbolic-ref", "--short", "HEAD") == "local-feature"
    backup, = (target / "data/code-update-backups").iterdir()
    state = json.loads((backup / "git-state.json").read_text())
    assert git(target, "rev-parse", state["backup_ref"]) == old_head
    assert (backup / "qq_bot/run.py").read_text() == "unstaged bot"
    assert (backup / "qq_bot/obsolete.py").read_text() == "old module"
    assert b"staged bot" in (backup / "staged.patch").read_bytes()
    assert b"unstaged bot" in (backup / "working.patch").read_bytes()


def test_force_git_handles_unrelated_history_and_previously_tracked_private_files(git_installation, tmp_path):
    source, _ = git_installation
    target = tmp_path / "legacy"
    target.mkdir()
    init_git(target)
    private = {
        ".env": b"local credential", "secrets/key.txt": b"local secret",
        "data/live.db": b"live data", "docs/DSH人设.md": b"local persona",
        "qq_bot/cordis.patch.yml": b"local profile",
        "tools/CORDIS.YML": b"local profile with uppercase filename",
        "assets/instruction.local.png": b"custom poster",
        ".venvs/local/marker": b"local Python environment",
        ".tools/browser-profile/cookies": b"local browser session",
        "昵称绑定.txt": b"local nickname bindings",
        "switch_presence.json": b"local Switch configuration",
    }
    write_files(target, {**release_files(), **private, "README.md": b"independent version"})
    git(target, "add", ".")
    git(target, "commit", "-m", "independent history")
    (target / ".env").write_bytes(b"PRIVATE_STAGED_SENTINEL")
    git(target, "add", ".env")
    private[".env"] = b"PRIVATE_WORKING_SENTINEL"
    (target / ".env").write_bytes(private[".env"])
    updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert all((target / name).read_bytes() == content for name, content in private.items())
    backup, = (target / "data/code-update-backups").iterdir()
    patches = (backup / "staged.patch").read_bytes() + (backup / "working.patch").read_bytes()
    assert b"PRIVATE_STAGED_SENTINEL" not in patches
    assert b"PRIVATE_WORKING_SENTINEL" not in patches


@pytest.mark.parametrize("failure", ["copy", "git-reset"])
def test_failed_force_git_restores_code_head_and_exact_staged_index(git_installation, monkeypatch, failure):
    _, target = git_installation
    (target / "README.md").write_text("local staged readme")
    git(target, "add", "README.md")
    (target / "README.md").write_text("local unstaged readme")
    old_head = git(target, "rev-parse", "HEAD")
    old_index = (target / ".git/index").read_bytes()
    if failure == "copy":
        original_replace = updater.os.replace
        def replace(source, destination):
            if destination == target / "assets/instruction.png":
                raise PermissionError("locked target")
            return original_replace(source, destination)
        monkeypatch.setattr(updater.os, "replace", replace)
    else:
        original_git = updater.run_git
        def fail_after_reset(root, *args):
            result = original_git(root, *args)
            if args[:2] == ("reset", "--mixed"):
                raise updater.UpdateError("simulated failure after Git ref changed")
            return result
        monkeypatch.setattr(updater, "run_git", fail_after_reset)
    with pytest.raises(updater.UpdateError, match="Git 状态已恢复"):
        updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == old_head
    assert (target / ".git/index").read_bytes() == old_index
    assert (target / "README.md").read_text() == "local unstaged readme"
    assert (target / "qq_bot/obsolete.py").read_bytes() == b"old module"
    assert not (target / "assets/instruction.png").exists()
    assert (target / ".env").read_text() == "private credentials"


def test_force_git_rejects_active_index_lock_without_touching_code(git_installation):
    _, target = git_installation
    (target / ".git/index.lock").write_bytes(b"another Git process")
    with pytest.raises(updater.UpdateError, match="索引正在使用"):
        updater.update_git(target)
    assert (target / "README.md").read_text() == "first"
    assert not (target / "data/code-update-backups").exists()


def test_force_git_aligns_history_and_staging_when_work_files_already_match(git_installation):
    source, target = git_installation
    updater.update_git(target)
    git(target, "commit", "--allow-empty", "-m", "local-only history")
    previous = git(target, "rev-parse", "HEAD")
    (target / "README.md").write_text("staged only")
    git(target, "add", "README.md")
    (target / "README.md").write_text("second")
    updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(target, "diff", "--cached", "--name-only") == ""
    assert (target / "README.md").read_text() == "second"
    backups = list((target / "data/code-update-backups").iterdir())
    matching = [item for item in backups if json.loads((item / "git-state.json").read_text())["previous_head"] == previous]
    assert len(matching) == 1
    assert b"staged only" in (matching[0] / "staged.patch").read_bytes()


@pytest.mark.parametrize("mode", ["detached", "unborn"])
def test_force_git_handles_detached_head_and_new_git_installations(git_installation, tmp_path, mode):
    source, target = git_installation
    if mode == "detached":
        git(target, "checkout", "--detach")
    else:
        target = tmp_path / "new-git"
        target.mkdir()
        init_git(target)
        write_files(target, {**release_files(), ".env": b"preserve local configuration"})
        git(target, "add", "qq_bot/run.py")
    updater.update_git(target)
    assert git(target, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    if mode == "detached":
        with pytest.raises(subprocess.CalledProcessError):
            git(target, "symbolic-ref", "--quiet", "HEAD")
    else:
        assert (target / ".env").read_bytes() == b"preserve local configuration"


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher required")
@pytest.mark.parametrize("folder", ["installation with spaces", "installation with bang!"])
def test_launcher_survives_replacing_itself_and_preserves_failure_code(tmp_path, folder):
    root = tmp_path / folder
    wrapper = root / "scripts/windows/update-project.cmd"
    wrapper.parent.mkdir(parents=True)
    original = Path(__file__).resolve().parents[1] / "scripts/windows/update-project.cmd"
    wrapper.write_bytes(original.read_bytes())
    (root / "tools").mkdir()
    (root / "tools/select_python.cmd").write_text(
        '@echo off\nset "BOT_PYTHON=' + sys.executable + '"\nexit /b 0\n', encoding="utf-8",
    )
    (root / "tools/update_project.py").write_text(
        'from pathlib import Path\nimport sys\n'
        'target = Path(__file__).resolve().parents[1] / "scripts/windows/update-project.cmd"\n'
        'target.write_text("@echo off\\necho replaced\\nexit /b 0\\n")\n'
        'sys.exit(7)\n', encoding="utf-8",
    )
    result = subprocess.run(["cmd", "/d", "/c", str(wrapper)], capture_output=True, timeout=30)
    assert result.returncode == 7
    assert b"replaced" in wrapper.read_bytes()
