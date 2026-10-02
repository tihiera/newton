"""Rules of the SSH layer that must not regress silently (each was once broken or
untested): what every ssh invocation carries, what key capture accepts, and how
values coming back from the host are parsed."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import HOSTKEY
from newton_agentd.runners.base import RunnerError
from newton_agentd.runners.ssh import (
    BOOTSTRAP_ERRORS,
    CAPTURE_SCRIPT,
    HARDENING_OPTS,
    ResolvedTarget,
    SshClient,
    SshRunner,
    SshTarget,
    Tunnel,
    parse_keyscan,
    split_paths,
)


def opts_of(argv: list[str]) -> set[str]:
    return {argv[i + 1] for i, a in enumerate(argv) if a == "-o"}


class RecordingClient(SshClient):
    """Records every argv; the 'host' answers from a script."""

    def __init__(self, tmp: Path, replies: dict[str, tuple[int, str, str]] | None = None):
        super().__init__(SshTarget("spark"), tmp / "newton_known_hosts", ssh="ssh")
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    async def resolve(self) -> ResolvedTarget:
        return ResolvedTarget("spark", 22, "u", ["/etc/ssh/ssh_known_hosts"], proxy=False)

    async def _exec(  # type: ignore[override]
        self, argv: list[str], stdin: bytes | None = None, timeout: float = 60
    ) -> tuple[int, str, str]:
        self.calls.append(argv)
        for needle, reply in self.replies.items():
            if needle in " ".join(argv):
                return reply
        return 255, "", "Host key verification failed."


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_every_scripted_call_is_hardened(tmp_path: Path) -> None:
    client = RecordingClient(tmp_path)
    hardening = set(HARDENING_OPTS[1::2])
    with pytest.raises(RunnerError):
        run(client.run("true"))
    assert hardening <= opts_of(client.calls[-1])
    assert "ClearAllForwardings=yes" in opts_of(client.calls[-1])

    with pytest.raises(RunnerError):
        run(client.scan_host_keys())
    scan = opts_of(client.calls[0 if len(client.calls) == 1 else -1])
    assert hardening <= scan
    # Capture must never trust or authenticate: strict, empty known_hosts.
    assert {"StrictHostKeyChecking=yes", "UserKnownHostsFile=/dev/null",
            "GlobalKnownHostsFile=/dev/null", "ClearAllForwardings=yes"} <= scan  # fmt: skip
    assert not any(o.startswith("StrictHostKeyChecking=") and o.endswith(("no", "accept-new"))
                   for o in scan)  # fmt: skip


def test_tunnel_master_and_mux_calls(tmp_path: Path) -> None:
    client = RecordingClient(tmp_path)
    started: list[list[str]] = []

    async def fake_start(self: Tunnel, argv: list[str], timeout: float) -> None:
        started.append(argv)
        raise RunnerError("stop here", code="unreachable")

    original = Tunnel.start
    Tunnel.start = fake_start  # type: ignore[method-assign]
    try:
        with pytest.raises(RunnerError):
            run(client.open_tunnel("/home/u/.newton/worker.sock"))
    finally:
        Tunnel.start = original  # type: ignore[method-assign]
    master = opts_of(started[0])
    wanted = {"ControlMaster=yes", "ClearAllForwardings=yes", "Tunnel=no",
              "StreamLocalBindMask=0177", "ForwardAgent=no", "RemoteCommand=none"}  # fmt: skip
    assert wanted <= master
    assert "ControlMaster=no" not in master and "ControlPath=none" not in master
    flags = started[0]
    assert all(flags[i + 1] != "-o" for i, a in enumerate(flags) if a == "-o")  # no dangling -o
    mux = Tunnel("ssh", "spark", None)._mux("-O", "check")
    assert mux[:3] == ["ssh", "-F", "/dev/null"]  # never re-sends the config's forwards


def test_capture_script_records_only_real_keys(tmp_path: Path) -> None:
    script = tmp_path / "capture"
    script.write_text(CAPTURE_SCRIPT)
    script.chmod(0o700)
    for args in (["ORDER", "spark", "NONE", "NONE"], ["HOSTNAME", "spark", "ssh-ed25519", HOSTKEY]):
        out = subprocess.run([str(script), *args], capture_output=True, text=True)
        assert out.stdout == ""  # prints nothing: ssh's verification must still fail
    assert (tmp_path / "keys").read_text().splitlines() == [f"spark ssh-ed25519 {HOSTKEY}"]


def test_key_parsing_rejects_placeholders_and_mismatched_blobs() -> None:
    rsa_blob = base64.b64encode(b"\x00\x00\x00\x07ssh-rsa" + b"x" * 20).decode()
    text = "\n".join(
        [
            "spark NONE NONE",
            f"spark ssh-ed25519 {rsa_blob}",  # type says ed25519, blob says rsa
            f"spark ssh-ed25519 {HOSTKEY}",
            f"spark ssh-ed25519 {HOSTKEY}",  # duplicate
        ]
    )
    keys = parse_keyscan(text)
    assert [(k.key_type, k.line) for k in keys] == [("ssh-ed25519", f"spark ssh-ed25519 {HOSTKEY}")]


def test_spaced_paths_from_ssh_g_are_rejoined() -> None:
    with tempfile.TemporaryDirectory() as d:
        spaced = Path(d) / "Application Support" / "kh"
        spaced.parent.mkdir()
        spaced.write_text("")
        value = f"{spaced} /etc/ssh/ssh_known_hosts2"
        assert split_paths(value) == [str(spaced), "/etc/ssh/ssh_known_hosts2"]


def test_remote_home_survives_rc_file_chatter(tmp_path: Path) -> None:
    client = RecordingClient(
        tmp_path, {"NEWTON-HOME": (0, "Welcome to DGX OS\nNEWTON-HOME:/home/u:END\n", "")}
    )
    assert run(client.remote_home()) == "/home/u"
    bad = RecordingClient(tmp_path, {"NEWTON-HOME": (0, "no markers here\n", "")})
    with pytest.raises(RunnerError) as e:
        run(bad.remote_home())
    assert e.value.code == "config"


def test_bootstrap_without_numpy_is_refused(tmp_path: Path) -> None:
    status = {"running": True, "deps": {"numpy": None}}
    client = RecordingClient(
        tmp_path,
        {
            "tar -xzf": (0, "", ""),
            "tokens": (0, "", ""),
            "bootstrap.sh": (0, json.dumps(status) + "\n", ""),
        },
    )
    worker_dir = Path(__file__).resolve().parents[2] / "worker"
    with pytest.raises(RunnerError) as e:
        run(client.bootstrap(worker_dir, "tok", python="python3"))
    assert e.value.code == "deps_missing"


def test_busy_and_rolled_back_are_transient() -> None:
    assert BOOTSTRAP_ERRORS[75][1] is True
    assert BOOTSTRAP_ERRORS[76][1] is True
    assert BOOTSTRAP_ERRORS[68][1] is False


def test_submit_waits_for_an_upgrade_across_protocol_versions(tmp_path: Path) -> None:
    worker_dir = Path(__file__).resolve().parents[2] / "worker"
    runner = SshRunner("h", RecordingClient(tmp_path), "tok", worker_dir)

    async def ready() -> None:
        pass

    runner.ensure_ready = ready  # type: ignore[method-assign]
    runner.protocol_mismatch = True
    with pytest.raises(RunnerError) as e:
        run(runner.submit({}, b""))
    assert e.value.code == "worker_upgrade_pending" and e.value.transient


def test_agentd_expects_the_protocol_this_worker_speaks() -> None:
    """agentd keeps its own copy (it never imports the worker): they must agree, or
    every host looks like it runs an old worker and new jobs wait forever."""
    import re

    from newton_agentd.runners.ssh import WORKER_PROTOCOL

    source = (Path(__file__).resolve().parents[2] / "worker" / "newton_worker" /
              "__init__.py").read_text()  # fmt: skip
    found = re.search(r"^PROTOCOL_VERSION = (\d+)$", source, re.M)
    assert found and int(found.group(1)) == WORKER_PROTOCOL
