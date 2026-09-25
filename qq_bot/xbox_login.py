from __future__ import annotations

import asyncio
import http.server
import os
import queue
import secrets
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .interprocess_lock import AsyncInterProcessFileLock, atomic_write_text, lock_path_for


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_PATH = PROJECT_ROOT / "secrets" / "xbox-tokens.json"
CLIENT_ID = "388ea51c-0b25-4029-aae2-17df49d23905"
REDIRECT_URI = "http://localhost:8080/auth/callback"


def token_path() -> Path:
    configured = os.getenv("XBOX_TOKENS_FILE", "").strip()
    if not configured:
        return DEFAULT_TOKEN_PATH
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def save_token(path: Path, value: str) -> None:
    atomic_write_text(path, value)


async def login() -> None:
    try:
        from xbox.webapi.api.client import XboxLiveClient
        from xbox.webapi.authentication.manager import AuthenticationManager
        from xbox.webapi.authentication.models import OAuth2TokenResponse
        from xbox.webapi.common.signed_session import SignedSession
    except ImportError as exc:
        raise RuntimeError("缺少 xbox-webapi，请先在机器人管理器中更新 Python 依赖。") from exc

    path = token_path()
    callback_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)
    expected_state = secrets.token_urlsafe(32)

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            state = (query.get("state") or [""])[0]
            code = (query.get("code") or [""])[0]
            error = (query.get("error_description") or query.get("error") or [""])[0]
            if state != expected_state:
                status, message = 400, "登录校验失败，请关闭页面后重新运行 Xbox 登录。"
            elif error:
                status, message = 400, f"Xbox 登录失败：{error}"
                callback_queue.put_nowait(("error", error))
            elif not code:
                status, message = 400, "没有收到登录授权码，请重新运行 Xbox 登录。"
            else:
                status, message = 200, "Xbox 登录成功，可以关闭这个页面。"
                callback_queue.put_nowait(("code", code))
            body = f"<!doctype html><meta charset='utf-8'><title>Xbox 登录</title><style>body{{font:20px 'Microsoft YaHei';background:#071019;color:#eef6fc;padding:60px;text-align:center}}</style><p>{message}</p>".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), CallbackHandler)
    except OSError as exc:
        raise RuntimeError("本机 8080 端口被占用，请关闭占用它的程序后重试。") from exc

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        async with SignedSession() as session:
            manager = AuthenticationManager(session, CLIENT_ID, "", REDIRECT_URI)
            if path.is_file():
                # The bot and this login helper share one rotating refresh
                # token. Hold the same OS-level lock while reading, refreshing,
                # validating presence, and saving so neither process can use a
                # stale token or overwrite a newer one.
                async with AsyncInterProcessFileLock(lock_path_for(path)):
                    try:
                        manager.oauth = OAuth2TokenResponse.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                        await manager.refresh_tokens()
                        if manager.oauth is not None:
                            # Save immediately after refresh. A later presence
                            # request may fail transiently and must not discard
                            # a newly rotated refresh token.
                            save_token(path, manager.oauth.model_dump_json())
                    except Exception:
                        manager.oauth = None
                        manager.user_token = None
                        manager.xsts_token = None
                    if manager.xsts_token and manager.xsts_token.is_valid():
                        client = XboxLiveClient(manager)
                        presence = await client.presence.get_presence_own()
                        del presence
                        gamertag = manager.xsts_token.gamertag if manager.xsts_token else "观察账号"
                        print()
                        print(f"Xbox 已登录：{gamertag}")
                        print(f"令牌已保存：{path}")
                        print("现在重启 QQ Bot，再在群里输入 /xbox status。")
                        return

            if not (manager.xsts_token and manager.xsts_token.is_valid()):
                url = manager.generate_authorization_url(state=expected_state)
                print("即将打开微软登录页面，请登录用于观察 Xbox 状态的账号。")
                print("如果浏览器没有自动打开，请复制下面的网址：")
                print(url)
                webbrowser.open(url)
                try:
                    kind, value = await asyncio.to_thread(callback_queue.get, True, 300)
                except queue.Empty as exc:
                    raise RuntimeError("等待微软登录超时，请重新运行 Xbox 登录。") from exc
                if kind != "code":
                    raise RuntimeError(f"微软登录失败：{value}")
                async with AsyncInterProcessFileLock(lock_path_for(path)):
                    await manager.request_tokens(value)
                    if manager.oauth is None:
                        raise RuntimeError("微软登录没有返回有效令牌。")
                    save_token(path, manager.oauth.model_dump_json())
                    client = XboxLiveClient(manager)
                    presence = await client.presence.get_presence_own()
                    del presence

            gamertag = manager.xsts_token.gamertag if manager.xsts_token else "观察账号"
            print()
            print(f"Xbox 已登录：{gamertag}")
            print(f"令牌已保存：{path}")
            print("现在重启 QQ Bot，再在群里输入 /xbox status。")
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    try:
        asyncio.run(login())
    except KeyboardInterrupt:
        print("Xbox 登录已取消。")
        raise SystemExit(130)
    except Exception as exc:
        print()
        print(f"Xbox 登录失败：{exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
