"""CLI: `python -m newton_worker {serve,status,stop,active-jobs,hardware,gpu}`."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import gpu, source_digest
from .fsutil import read_json
from .hardware import bootstrap_info, probe
from .jobs import DEFAULT_ALLOWED_MODULES, JobStore, pid_alive, process_command_line
from .server import Tokens, serve


def default_root() -> Path:
    return Path(os.environ.get("NEWTON_HOME", Path.home() / ".newton"))


def read_tokens(args: argparse.Namespace) -> Tokens:
    env_token = os.environ.get("NEWTON_WORKER_TOKEN")
    if env_token:
        return Tokens(fixed=env_token)
    if args.token_file:
        token = Path(args.token_file).read_text().strip()
        if not token:
            sys.exit(f"empty token file: {args.token_file}")
        return Tokens(fixed=token)
    tokens = Tokens(root=args.root)
    if not tokens._files():
        sys.exit(f"no tokens in {args.root / 'tokens'}")
    return tokens


def detach() -> None:
    """Become a daemon (double fork + setsid): survives the SSH session that started
    it, needs no nohup/setsid binaries and no controlling terminal (macOS nohup
    refuses to run without one). stdout/stderr stay where the caller redirected them."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)


def cmd_serve(args: argparse.Namespace) -> None:
    if args.detach:
        detach()
    allowed = tuple(m for m in args.allowed_modules.split(",") if m)
    socket_path = Path(args.socket) if args.socket else None
    server = serve(
        args.root, read_tokens(args), args.host, args.port, allowed, socket_path=socket_path
    )
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if socket_path is not None:
        listening: dict[str, Any] = {"event": "listening", "socket": str(socket_path)}
    else:
        listening = {"event": "listening", "port": server.server_address[1]}
    print(json.dumps(listening), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        if socket_path is not None:
            socket_path.unlink(missing_ok=True)
        _forget_worker(args.root)


def _forget_worker(root: Path) -> None:
    """Remove worker.json if it still describes this process, so a later reboot
    can't leave behind a pid that now belongs to something else."""
    path = root / "worker.json"
    try:
        if read_json(path).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def is_worker_process(pid: Any, root: Path) -> bool:
    """True only if pid is alive and is a worker serving this root: a stale
    worker.json pid may belong to an unrelated process after a reboot."""
    if not isinstance(pid, int) or not pid_alive(pid):
        return False
    cmdline = process_command_line(pid)
    same_root = str(root) in cmdline or os.path.realpath(root) in cmdline
    return "newton_worker" in cmdline and " serve" in cmdline and same_root


def _worker_info(root: Path) -> dict[str, Any]:
    path = root / "worker.json"
    if not path.exists():
        return {"running": False}
    info = read_json(path)
    info["running"] = is_worker_process(info.get("pid"), root)
    return info


def _importable_versions() -> dict[str, Any]:
    """Actually import the benchmark dependencies: a present-but-broken
    install must not count as installed."""
    out: dict[str, Any] = {}
    for name in ("numpy", "matplotlib"):
        try:
            module = importlib.import_module(name)
        except Exception:
            out[name] = None
        else:
            out[name] = str(getattr(module, "__version__", "unknown"))
    return out


def cmd_status(args: argparse.Namespace) -> None:
    info = _worker_info(args.root)
    info.update(
        python=sys.executable,
        python_version=platform.python_version(),
        deps=_importable_versions(),
        bootstrap=bootstrap_info(args.root),
        source_digest=source_digest(),
    )
    print(json.dumps(info))


def _wait_gone(pid: int, root: Path, seconds: float) -> bool:
    # "Gone" means no longer a live worker: a zombie whose parent hasn't reaped
    # it yet still answers kill(0) but has no command line any more.
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not is_worker_process(pid, root):
            return True
        time.sleep(0.1)
    return not is_worker_process(pid, root)


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop the worker for this root. Exit 1 if it is still alive afterwards."""
    info = _worker_info(args.root)
    if not info.get("running"):
        print(json.dumps({"stopped": False, "was_running": False}))
        return
    pid = info["pid"]
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        if _wait_gone(pid, args.root, wait):
            break
    gone = not is_worker_process(pid, args.root)
    print(json.dumps({"stopped": gone, "was_running": True, "pid": pid}))
    if not gone:
        sys.exit(1)


def cmd_active_jobs(args: argparse.Namespace) -> None:
    """Number of jobs whose wrapper is still running (dead wrappers become lost)."""
    jobs = JobStore(args.root).list()
    print(sum(1 for j in jobs if j["state"] in ("starting", "running")))


def cmd_hardware(args: argparse.Namespace) -> None:
    print(json.dumps(probe(args.root), indent=2))


def cmd_gpu(args: argparse.Namespace) -> None:
    if not args.gpu_args:
        sys.exit("usage: newton_worker gpu {plan,smoke,defer,fail} [...]")
    sys.exit(gpu.main(args.gpu_args, args.root))


def main() -> None:
    parser = argparse.ArgumentParser(prog="newton_worker")
    parser.add_argument("--root", type=Path, default=default_root())
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8777)
    p.add_argument("--socket", help="serve on this Unix socket instead of TCP")
    p.add_argument("--detach", action="store_true", help="run as a daemon")
    p.add_argument("--token-file")
    p.add_argument("--allowed-modules", default=",".join(DEFAULT_ALLOWED_MODULES))
    p.set_defaults(func=cmd_serve)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("active-jobs").set_defaults(func=cmd_active_jobs)
    sub.add_parser("hardware").set_defaults(func=cmd_hardware)
    p = sub.add_parser("gpu", help="GPU support steps, driven by bootstrap.sh")
    p.add_argument("gpu_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_gpu)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    args.func(args)


if __name__ == "__main__":
    main()
