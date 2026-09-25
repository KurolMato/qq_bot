"""Refresh registered PSN avatars without sending group messages."""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from qq_bot.ps5_registry import Ps5PresenceMonitor, ps5_client, registry


async def main() -> None:
    if not ps5_client.configured:
        raise SystemExit("PSN credentials or dependency unavailable")
    before = {item.account_id: item.avatar_url for item in registry.list_all()}
    result = await Ps5PresenceMonitor(registry, ps5_client).refresh_avatars()
    result["changed"] = sum(
        before[item.account_id] != item.avatar_url
        for item in {row.account_id: row for row in registry.list_all()}.values()
        if item.account_id in before
    )
    print(result)
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
