from __future__ import annotations

import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = PROJECT_ROOT / "downloads"


def main() -> int:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in DOWNLOAD_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        elif child.is_file():
            child.unlink(missing_ok=True)
            removed += 1
    print(f"已清理 {removed} 个下载项目。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
