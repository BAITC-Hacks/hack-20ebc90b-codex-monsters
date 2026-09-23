"""Run the real API and buyer UI together, locally or in one container.

Install the project first (`uv sync --frozen`), then run
`uv run python scripts/run_app.py`. Both children use this Python environment.
"""

from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def source_directory(explicit: str | None, state: Path) -> Path | None:
    """Use an explicit local directory or the supplied repository workbooks."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("Каталог исходных данных не найден")
        return path
    local = ROOT / "data" / "sources"
    if local.is_dir():
        return local
    archives = [ROOT / name for name in ("IEK.zip", "Systeme electric.zip")]
    if not all(path.is_file() for path in archives):
        return None
    target = state / "source-files"
    for archive in archives:
        with ZipFile(archive) as zipped:
            for member in zipped.infolist():
                path = Path(member.filename)
                if member.is_dir() or path.suffix.lower() != ".xlsx":
                    continue
                if path.is_absolute() or ".." in path.parts or path.parts[0] not in {"IEK", "Systeme electric"}:
                    raise ValueError("В архиве указан недопустимый путь")
                if member.file_size > 100_000_000:
                    raise ValueError("Файл в архиве превышает допустимый размер")
                destination = (target / path).resolve()
                if not destination.is_relative_to(target.resolve()):
                    raise ValueError("Недопустимый путь распаковки")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    destination.write_bytes(zipped.read(member))
    return target


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="UI bind address (default: 127.0.0.1)")
    parser.add_argument(
        "--port", "--ui-port", type=port_number,
        default=os.environ.get("UI_PORT") or os.environ.get("PORT") or "8501",
        help="UI port (default: UI_PORT, PORT, or 8501)",
    )
    parser.add_argument(
        "--api-port", type=port_number, default=os.environ.get("API_PORT") or "8000",
        help="loopback API port (default: API_PORT or 8000)",
    )
    parser.add_argument(
        "--startup-timeout", type=float, default=60,
        help="seconds to wait for each service to become healthy (default: 60)",
    )
    parser.add_argument("--state-dir", type=Path, help="Каталог сохранённых планов и заказов")
    parser.add_argument("--source-root", default=os.environ.get("EKT_SOURCE_ROOT"),
                        help="Каталог с папками IEK и Systeme electric")
    args = parser.parse_args(argv)
    if not 0 < args.startup_timeout < float("inf"):
        parser.error("--startup-timeout must be a positive finite number")
    if args.port == args.api_port:
        parser.error("UI and API must use different ports")
    return args


def require_free_port(host: str, port: int) -> None:
    """Fail before starting if an existing service already owns either port."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    family, kind, protocol, _, address = addresses[0]
    try:
        with socket.socket(family, kind, protocol) as listener:
            if os.name == "posix":
                # Match the servers: old connections in TIME_WAIT may remain
                # after a clean stop, and must not prevent an immediate restart.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(1)
    except OSError as exc:
        raise RuntimeError(f"Cannot bind {host}:{port}: {exc}") from exc


def check_children(children: list[tuple[str, subprocess.Popen]]) -> None:
    for name, child in children:
        code = child.poll()
        if code is not None:
            raise RuntimeError(f"{name} exited unexpectedly (code {code}); stopping the application")


def wait_healthy(url, children, stopped, timeout, *, api=False) -> bool:
    # Loopback readiness must not depend on HTTP_PROXY settings in the shell.
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while not stopped():
        check_children(children)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Service did not become healthy within {timeout:g}s: {url}")
        try:
            with opener.open(url, timeout=min(0.5, timeout)) as response:
                body = response.read(4096)
                ready = response.status == 200 and (
                    json.loads(body).get("status") == "ok" if api else body.strip() == b"ok"
                )
            check_children(children)
            if ready:
                return True
        except (URLError, OSError, HTTPException, ValueError, AttributeError):
            pass
        time.sleep(0.1)
    return False


def stop_children(children: list[tuple[str, subprocess.Popen]]) -> None:
    """Terminate both services and their process groups, then reap our children."""
    for _, child in reversed(children):
        try:
            if os.name == "posix":
                # A child that already exited can still have living descendants.
                os.killpg(child.pid, signal.SIGTERM)
            elif child.poll() is None:
                child.terminate()
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 8
    for _, child in reversed(children):
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for _, child in reversed(children):
        try:
            if os.name == "posix":
                # Also remove descendants left after the direct child exited.
                os.killpg(child.pid, signal.SIGKILL)
            elif child.poll() is None:
                child.kill()
        except ProcessLookupError:
            pass
        child.wait()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = os.environ.copy()
    state = (args.state_dir or Path(env.get("EKT_DATA_DIR") or ROOT / "var")).expanduser().resolve()
    env["EKT_DATA_DIR"] = str(state)
    env["BUYER_UI_MODE"] = "http"
    env["BUYER_API_URL"] = f"http://127.0.0.1:{args.api_port}"
    env["PYTHONUNBUFFERED"] = "1"
    # Persisted workspace chooses the latest work, never old development IDs.
    for key in ("BUYER_SNAPSHOT_ID", "BUYER_RUN_ID"):
        env.pop(key, None)
    children: list[tuple[str, subprocess.Popen]] = []
    stop_signal = None

    def request_stop(signum, _frame):
        nonlocal stop_signal
        stop_signal = signum

    previous_handlers = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }

    def start(name: str, command: list[str]) -> None:
        if stop_signal is not None:
            return
        child = subprocess.Popen(
            [sys.executable, "-m", *command], cwd=ROOT, env=env,
            start_new_session=os.name == "posix",
        )
        children.append((name, child))
        print(f"Started {name} (PID {child.pid})", flush=True)

    try:
        require_free_port("127.0.0.1", args.api_port)
        require_free_port(args.host, args.port)
        sources = source_directory(args.source_root, state)
        if sources is not None:
            env["EKT_SOURCE_ROOT"] = str(sources)
        start("API", [
            "uvicorn", "ekt.api.app:app", "--host", "127.0.0.1", "--port", str(args.api_port),
        ])
        if not wait_healthy(
            f"{env['BUYER_API_URL']}/v1/health", children,
            lambda: stop_signal is not None, args.startup_timeout, api=True,
        ):
            return 128 + stop_signal
        start("buyer UI", [
            "streamlit", "run", "apps/buyer_ui/app.py", "--server.address", args.host,
            "--server.port", str(args.port), "--server.headless", "true",
            "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false",
            "--client.toolbarMode", "minimal",
        ])
        local_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
        local_host = f"[{local_host}]" if ":" in local_host else local_host
        ui_url = f"http://{local_host}:{args.port}"
        if not wait_healthy(
            f"{ui_url}/_stcore/health", children,
            lambda: stop_signal is not None, args.startup_timeout,
        ):
            return 128 + stop_signal
        print(f"Application ready: {ui_url} (API: {env['BUYER_API_URL']})", flush=True)
        while stop_signal is None:
            check_children(children)
            time.sleep(0.2)
        return 128 + stop_signal
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Startup/runtime error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_children(children)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
