"""Update the installed bot from the official repository without replacing local state."""
from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
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


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    if result.returncode:
        raise UpdateError(result.stderr.strip() or result.stdout.strip() or "Git 操作失败")
    return result.stdout.strip()


def update_git(root: Path) -> None:
    if shutil.which("git") is None:
        raise UpdateError("找不到 Git，请先在管理器中安装 Windows 基础软件。")
    if Path(run_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("当前目录不是独立的项目 Git 根目录，已取消更新。")
    if run_git(root, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("当前代码有未提交修改，已取消更新。请运行 git status 检查，不要强制重置。")
    if run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD") != BRANCH:
        raise UpdateError(f"当前不在 {BRANCH} 分支，已取消更新；请先检查并切回该分支。")
    # Use the explicit trusted source, never whichever remote/upstream happens to be configured.
    run_git(root, "fetch", "--no-tags", REPOSITORY_URL, f"refs/heads/{BRANCH}")
    try:
        run_git(root, "merge", "--ff-only", "FETCH_HEAD")
    except UpdateError as exc:
        raise UpdateError(
            "无法快进到官方 main（可能有本地提交，或旧仓库历史不同），未强制覆盖代码。"
            "请保留配置和数据后按 README 的仓库迁移步骤处理。\n" + str(exc)
        ) from exc


def download_archive() -> bytes:
    request = Request(ARCHIVE_URL, headers={"User-Agent": "qq-bot-updater/1.0"})
    with urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise UpdateError("下载包过大，已取消更新。")
    return data


def managed_file(path: PurePosixPath) -> bool:
    if len(path.parts) == 1:
        return path.name in ROOT_FILES
    if path.parts[0] not in CODE_DIRS:
        return False
    if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in path.parts):
        return False
    if path.name in {"cordis.patch.yml", "cordis.yml"} or "人设" in path.name:
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


def install_archive(root: Path, content: bytes) -> tuple[int, Path | None]:
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
            # Operators may provide their own instruction poster.
            if relative.as_posix() == "assets/instruction.png" and target.exists():
                continue
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
        if not files:
            return 0, None
        relative_backup = Path("data/code-update-backups") / (
            datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        )
        backup = safe_destination(root, relative_backup)
        backup.mkdir(parents=True)
        originals: set[Path] = set()
        for relative in files:
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
        except (OSError, UpdateError) as exc:
            for relative in reversed(applied):
                target = safe_destination(root, relative)
                if relative in originals:
                    shutil.copy2(backup / relative, target)
                else:
                    target.unlink(missing_ok=True)
            raise UpdateError(f"更新失败，已恢复已替换的代码。备份：{backup}") from exc
        return len(files), backup


def update(root: Path) -> None:
    root = root.resolve()
    if not (root / "机器人管理.cmd").is_file() or not (root / "qq_bot").is_dir():
        raise UpdateError("找不到项目入口，请在完整的项目目录中运行。")
    print(f"更新源：{REPOSITORY_URL}（{BRANCH}）")
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
