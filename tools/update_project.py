"""Force-sync official program files while preserving local configuration and runtime state."""
from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
from urllib.request import Request, urlopen
import uuid
import zipfile

REPOSITORY_URL = "https://github.com/KurolMato/qq_bot.git"
BRANCH = "main"
ARCHIVE_URL = "https://codeload.github.com/KurolMato/qq_bot/zip/refs/heads/main"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
CODE_DIRS = {"app", "qq_bot", "static", "scripts", "tools", "docs", "assets", "tests", "examples"}
ROOT_FILES = {
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "机器人管理.cmd",
    ".env.example", ".gitignore", ".gitattributes", ".dockerignore", "Dockerfile", "compose.yaml",
    "requirements.txt", "requirements-bot.txt", "requirements-dev.txt", "requirements-douyin-note.txt",
}


class UpdateError(RuntimeError):
    pass


def git_bytes(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=180,
    )
    if result.returncode:
        detail = result.stderr or result.stdout
        raise UpdateError(detail.decode("utf-8", errors="replace").strip() or "Git 操作失败")
    return result.stdout


def run_git(root: Path, *args: str) -> str:
    return git_bytes(root, *args).decode("utf-8", errors="replace").strip()


def code_diff(root: Path, names: list[str], *, staged: bool = False) -> bytes:
    # Private tracked files from legacy repositories remain on disk; do not
    # duplicate their credentials or database contents into code patch backups.
    chunks = []
    flags = ["--cached"] if staged else []
    for start in range(0, len(names), 32):
        chunks.append(git_bytes(root, "diff", *flags, "--binary", "--", *names[start:start + 32]))
    return b"".join(chunks)


def update_git(root: Path) -> None:
    if shutil.which("git") is None:
        raise UpdateError("找不到 Git，请先在管理器中安装 Windows 基础软件。")
    if Path(run_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("当前目录不是独立的项目 Git 根目录，已取消更新。")
    # Use the explicit trusted source, never whichever remote/upstream happens to be configured.
    run_git(root, "fetch", "--no-tags", REPOSITORY_URL, f"refs/heads/{BRANCH}")
    target = run_git(root, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
    try:
        previous = run_git(root, "rev-parse", "--verify", "HEAD")
    except UpdateError:
        previous = None
    try:
        head_ref = run_git(root, "symbolic-ref", "--quiet", "HEAD")
    except UpdateError:
        head_ref = None
    if previous is None and head_ref is None:
        raise UpdateError("无法识别当前 Git 状态，已取消更新。")
    old_paths = set(git_bytes(root, "ls-files", "-z").decode("utf-8").split("\0"))
    if previous:
        old_paths.update(git_bytes(root, "ls-tree", "-r", "--name-only", "-z", previous).decode("utf-8").split("\0"))
    obsolete_candidates = {Path(name) for name in old_paths if name and managed_file(PurePosixPath(name))}
    content = git_bytes(root, "archive", "--format=zip", "--prefix=qq-bot-update/", target)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise UpdateError("远程版本过大，已取消更新。")

    index = Path(run_git(root, "rev-parse", "--git-path", "index"))
    index = index if index.is_absolute() else root / index
    if index.with_name(index.name + ".lock").exists():
        raise UpdateError("Git 索引正在使用中，请等待其他 Git 操作结束后重试。")
    original_index = index.read_bytes() if index.exists() else None
    backup = new_backup(root)
    backup_ref = f"refs/heads/codex/update-backup-{backup.name}"
    if previous:
        run_git(root, "update-ref", backup_ref, previous)
    (backup / "git-state.json").write_text(json.dumps({
        "previous_head": previous, "previous_ref": head_ref, "target": target,
        "backup_ref": backup_ref if previous else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if original_index is not None:
        (backup / "git-index").write_bytes(original_index)
    code_names = sorted(path.as_posix() for path in obsolete_candidates)
    (backup / "staged.patch").write_bytes(code_diff(root, code_names, staged=True))
    (backup / "working.patch").write_bytes(code_diff(root, code_names))

    def align_git():
        # Mixed reset changes only HEAD/index. Work files are updated separately
        # through the protected file transaction, never reset --hard or clean.
        run_git(root, "reset", "--mixed", target)

    try:
        count, _ = install_archive(root, content, obsolete_candidates=obsolete_candidates,
                                   backup=backup, finalize=align_git)
    except (OSError, UpdateError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        try:
            if previous:
                run_git(root, "update-ref", "HEAD", previous)
            elif head_ref:
                run_git(root, "update-ref", "-d", head_ref)
            if original_index is None:
                index.unlink(missing_ok=True)
            else:
                index.write_bytes(original_index)
        except (OSError, UpdateError, subprocess.SubprocessError) as rollback_error:
            raise UpdateError(f"更新失败，Git 状态需从备份恢复：{backup}；{rollback_error}") from exc
        raise UpdateError(f"强制更新失败，Git 状态已恢复；旧代码与补丁备份：{backup}。{exc}") from exc
    print(f"Git 安装版：已强制同步 {count} 个程序文件，本地提交备份：{backup_ref if previous else '无旧提交'}")
    print(f"旧代码和 Git 状态备份：{backup}")


def download_archive() -> bytes:
    request = Request(ARCHIVE_URL, headers={"User-Agent": "qq-bot-updater/1.0"})
    with urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise UpdateError("下载包过大，已取消更新。")
    return data


def managed_file(path: PurePosixPath) -> bool:
    if path.as_posix().casefold() == "assets/instruction.local.png":
        return False
    if len(path.parts) == 1:
        return path.name in ROOT_FILES
    if path.parts[0] not in CODE_DIRS:
        return False
    if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in path.parts):
        return False
    if path.name.casefold() in {"cordis.patch.yml", "cordis.yml"} or "人设" in path.name:
        return False
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".log", ".pyc"}:
        return False
    return not (len(path.parts) > 1 and path.parts[:2] in {
        ("assets", "archive"), ("assets", "previews"),
    })


def safe_destination(root: Path, relative: Path) -> Path:
    target = root / relative
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise UpdateError("更新路径指向项目目录外，已取消更新。") from exc
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise UpdateError("更新路径包含本地符号链接或目录联接，已取消更新。")
    return target


def new_backup(root: Path) -> Path:
    relative = Path("data/code-update-backups") / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    )
    backup = safe_destination(root, relative)
    backup.mkdir(parents=True)
    return backup


def install_archive(root: Path, content: bytes, *, obsolete_candidates=(), backup=None,
                    finalize=None) -> tuple[int, Path | None]:
    """Validate the complete package, back up changed code, then apply with rollback."""
    with zipfile.ZipFile(BytesIO(content)) as package, tempfile.TemporaryDirectory(prefix="qq-bot-update-") as temp:
        stage = Path(temp)
        seen: set[str] = set()
        files: list[Path] = []
        total = 0
        prefixes: set[str] = set()
        for member in package.infolist():
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                    or ":" in member.filename or not path.parts):
                raise UpdateError("下载包包含不安全的路径，已取消更新。")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise UpdateError("下载包包含符号链接，已取消更新。")
            prefixes.add(path.parts[0])
            if len(prefixes) != 1:
                raise UpdateError("下载包目录结构异常，已取消更新。")
            total += member.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise UpdateError("下载包解压后过大，已取消更新。")
            if member.is_dir() or len(path.parts) < 2:
                continue
            relative = PurePosixPath(*path.parts[1:])
            if not managed_file(relative):
                continue
            identity = relative.as_posix().casefold()
            if identity in seen:
                raise UpdateError("下载包包含重名文件，已取消更新。")
            seen.add(identity)
            relative_path = Path(*relative.parts)
            target = safe_destination(root, relative_path)
            output = stage / relative_path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(package.read(member))
            if target.exists() and not target.is_file():
                raise UpdateError("目标文件被本地目录占用，已取消更新。")
            if not target.exists() or target.read_bytes() != output.read_bytes():
                files.append(relative_path)
        for required in ("机器人管理.cmd", "qq_bot/run.py", "scripts/windows/update-project.cmd",
                         "tools/update_project.py", "tools/select_python.cmd"):
            if not (stage / required).is_file():
                raise UpdateError("下载包缺少项目必要文件，已取消更新。")
        incoming = {name.casefold() for name in seen}
        removals = []
        for relative in sorted(obsolete_candidates):
            if not managed_file(PurePosixPath(relative.as_posix())) or relative.as_posix().casefold() in incoming:
                continue
            target = safe_destination(root, relative)
            if target.exists():
                if not target.is_file():
                    raise UpdateError("旧程序文件被本地目录占用，已取消更新。")
                removals.append(relative)
        changes = files + removals
        if not changes and finalize is None:
            return 0, None
        backup = backup or new_backup(root)
        originals: set[Path] = set()
        for relative in changes:
            target = safe_destination(root, relative)
            if target.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                originals.add(relative)
        applied: list[Path] = []
        try:
            for relative in files:
                target = safe_destination(root, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                # A same-directory temporary file makes replacement atomic on Windows.
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".update-", delete=False) as output:
                    pending = Path(output.name)
                try:
                    shutil.copy2(stage / relative, pending)
                    os.replace(pending, target)
                    applied.append(relative)
                finally:
                    pending.unlink(missing_ok=True)
            for relative in removals:
                safe_destination(root, relative).unlink()
                applied.append(relative)
            if finalize is not None:
                finalize()
        except (OSError, UpdateError, subprocess.SubprocessError) as exc:
            for relative in reversed(applied):
                target = safe_destination(root, relative)
                if relative in originals:
                    shutil.copy2(backup / relative, target)
                else:
                    target.unlink(missing_ok=True)
            raise UpdateError(f"更新失败，已恢复已替换的代码。备份：{backup}") from exc
        return len(changes), backup


def update(root: Path) -> None:
    root = root.resolve()
    if not (root / "机器人管理.cmd").is_file() or not (root / "qq_bot").is_dir():
        raise UpdateError("找不到项目入口，请在完整的项目目录中运行。")
    print(f"更新源：{REPOSITORY_URL}（{BRANCH}）")
    print("强制覆盖本机程序版本；原配置、密钥、人设和运行数据保留。")
    if (root / ".git").exists():
        update_git(root)
    else:
        count, backup = install_archive(root, download_archive())
        print(f"ZIP 安装版：更新 {count} 个程序文件。")
        if backup:
            print(f"旧代码备份：{backup}")
    print("更新完成；本机配置、密钥、人设和运行数据保持不变。")
    print("依赖清单如有变化，请选择菜单 5 更新依赖，再启动整套机器人。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        update(Path(__file__).resolve().parents[1])
    except (OSError, subprocess.SubprocessError, UpdateError, zipfile.BadZipFile) as exc:
        print(f"ERROR: 更新失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
