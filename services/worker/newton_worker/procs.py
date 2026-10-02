"""Process identity: signal a recorded process (group) only while it is provably
still ours.

A pid recorded before a reboot, or by a supervisor that died, may now belong to
anyone (a login shell, the worker itself, the user's own job). So each engine's
identity is recorded when it is spawned: the boot it ran in, and its start time.
Before Newton signals a process group it checks that identity:

  - another boot: nothing of ours survived it; leave everything alone;
  - leader alive with another start time: the pid was reused; leave it alone;
  - leader gone but the group alive: those are our engine's children (a pid that is
    still some group's id is never handed out again), so stop them too;
  - identity unknown: leave it alone (a missed stop is better than killing a stranger).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


def boot_id() -> Optional[str]:
    path = Path("/proc/sys/kernel/random/boot_id")
    if path.exists():
        try:
            return path.read_text().strip() or None
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return None
        return out or None
    return None


def start_time(pid: int) -> Optional[str]:
    """When the process started (clock ticks since boot on Linux), or None."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            return None
        return fields[19] if len(fields) > 19 else None  # field 22 of the whole line
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def identity(pid: int) -> dict[str, Any]:
    return {"boot_id": boot_id(), "start": start_time(pid)}


def last_argument_is(pid: int, path: Path) -> Optional[bool]:
    """Is `path` the last argument of the process's command line? None when the
    command line can't be read right now (don't treat a hiccup as "gone")."""
    wanted = {str(path), os.path.realpath(path)}
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        try:
            args = proc.read_bytes().split(b"\0")
        except OSError:
            return None
        args = [a for a in args if a]
        return bool(args) and args[-1].decode(errors="replace") in wanted
    if not Path("/proc").is_dir():
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5)  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return False  # no such process
        command = out.stdout.strip()
        return any(command.endswith(" " + w) for w in wanted)
    return False  # Linux and no /proc/<pid>: the process is gone


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but not ours to signal
    return True


def ours(pgid: Any, recorded: Optional[dict[str, Any]]) -> bool:
    """Is the process group `pgid` still the one recorded? See the module doc."""
    if not isinstance(pgid, int) or pgid <= 1 or not recorded:
        return False
    now_boot, then_boot = boot_id(), recorded.get("boot_id")
    if now_boot and then_boot and now_boot != then_boot:
        return False
    if not group_alive(pgid):
        return False
    started = start_time(pgid)
    if started is not None:  # the leader is alive: it must be the same process
        return bool(recorded.get("start")) and started == recorded.get("start")
    return True  # leader gone, group alive: its children


def stop_group(pgid: Any, recorded: Optional[dict[str, Any]], grace: float = 10.0) -> bool:
    """SIGTERM the group, wait up to `grace` for every member to exit, then SIGKILL
    whatever is left. Only if the group is provably ours. True if it was signalled."""
    if not ours(pgid, recorded):
        return False
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.time() + wait
        while time.time() < deadline:
            _reap(pgid)
            if not group_alive(pgid):
                return True
            time.sleep(0.1)
    return True


def _reap(pid: int) -> None:
    """Collect our own exited child (a zombie still counts as a group member)."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
