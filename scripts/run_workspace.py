"""Run the actual backend and built Nuxt locally with a server-only API token."""

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from audit_models import AuditSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=8100)
    parser.add_argument("--ui-port", type=int, default=3010)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    settings = AuditSettings.load()
    if (
        not settings.database_url
        or not settings.blob_store_url
        or not settings.api_token
    ):
        raise SystemExit(
            "Set DATABASE_URL, BLOB_STORE_URL and AUDIT_API_TOKEN in the ignored root .env"
        )
    node = shutil.which("node")
    entry = root / "frontend/.output/server/index.mjs"
    if not node or not entry.is_file():
        raise SystemExit(
            "Node and built frontend required: pnpm --dir frontend install --frozen-lockfile; pnpm --dir frontend build"
        )
    # Refuse to claim an unrelated running API as our child process.
    import socket

    for port in (args.api_port, args.ui_port):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise SystemExit(f"Local port {port} is already in use") from exc
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    logs = root / ".audit_cache/runtime"
    logs.mkdir(parents=True, exist_ok=True)
    processes = []
    try:
        with (
            (logs / "api.log").open("a", encoding="utf-8") as api_log,
            (logs / "frontend.log").open("a", encoding="utf-8") as ui_log,
        ):
            api = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "api:get_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.api_port),
                ],
                cwd=root,
                stdout=api_log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            processes.append(api)
            url = f"http://127.0.0.1:{args.api_port}"
            for _ in range(100):
                if api.poll() is not None:
                    raise SystemExit(
                        "API startup failed; see .audit_cache/runtime/api.log"
                    )
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1):
                        break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
            else:
                raise SystemExit(
                    "API startup timed out; see .audit_cache/runtime/api.log"
                )
            env = {
                **os.environ,
                "NUXT_BACKEND_URL": url,
                "NUXT_BACKEND_TOKEN": settings.api_token,
                "HOST": "127.0.0.1",
                "PORT": str(args.ui_port),
            }
            ui = subprocess.Popen(
                [node, str(entry)],
                cwd=root / "frontend",
                env=env,
                stdout=ui_log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            processes.append(ui)
            print(f"Workspace: http://127.0.0.1:{args.ui_port}/", flush=True)
            print(
                "Ctrl+C stops both child processes. Logs: .audit_cache/runtime",
                flush=True,
            )
            while all(p.poll() is None for p in processes):
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    # Include owned PDF subprocesses; they must not outlive this API.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        check=False,
                    )
                else:
                    process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
