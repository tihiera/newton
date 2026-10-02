"""Step 2 (G2): one-click connect, host discovery, and an SSH layer that works
with real-world ssh configs (NVIDIA Sync paths with spaces, ProxyCommand aliases,
hostile ~/.ssh/config options, sshd forwarding policy)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTH, FakeRemote, wait_job
from fastapi.testclient import TestClient
from newton_agentd.app import create_app
from newton_agentd.config import Settings
from newton_agentd.runners.ssh import SshClient, SshTarget

NO_DEPS = {"python": sys.executable, "use_venv": False, "install_deps": False}


def add_host(client: TestClient, alias: str = "spark", **extra: Any) -> dict[str, Any]:
    r = client.post("/hosts", json={"name": alias, "ssh_target": alias, **NO_DEPS, **extra})
    assert r.status_code == 201, r.text
    host: dict[str, Any] = r.json()
    return host


def test_connect_asks_for_trust_then_finishes_in_one_call(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "hostkey_unknown"
        fps = [k["fingerprint"] for k in body["fingerprints"]]
        assert fps and fps[0].startswith("SHA256:")
        assert not (fake_remote.fp / "worker.json").exists()  # nothing ran yet

        client.post(f"/hosts/{host['id']}/hostkeys/trust", json={"fingerprints": fps})
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200, r.text
        assert r.json()["host"]["status"] == "online"
        job = wait_job(client, r.json()["selftest_job_id"], {"succeeded", "failed"}, 60)
        assert job["state"] == "succeeded"

        # The worker listens on a Unix socket only its owner can reach.
        sock = fake_remote.fp / "worker.sock"
        assert sock.is_socket()
        assert sock.stat().st_mode & 0o077 == 0
        assert fake_remote.fp.stat().st_mode & 0o777 == 0o700


def test_key_in_users_known_hosts_with_spaces_needs_no_trust_step(
    ssh_settings: Settings, fake_remote: FakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NVIDIA Sync-style: the user's ssh config points at a known_hosts under
    # "Application Support", and pairing already put the Spark's key there.
    from conftest import HOSTKEY

    known = tmp_path / "Library" / "Application Support" / "NVIDIA" / "Sync" / "known_hosts"
    known.parent.mkdir(parents=True)
    known.write_text(f"spark ssh-ed25519 {HOSTKEY}\n")
    monkeypatch.setenv("FAKESSH_USER_KNOWN_HOSTS", str(known))
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200, r.text
    assert not ssh_settings.known_hosts_path.exists()  # nothing had to be trusted


def test_proxy_alias_key_is_captured_without_keyscan(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NVIDIA Sync's Tailscale mode: a ProxyCommand alias ssh-keyscan can't reach.
    monkeypatch.setenv("FAKESSH_PROXY", "1")
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        keys = client.get(f"/hosts/{host['id']}/hostkeys")
        assert keys.status_code == 200, keys.text
        fps = [k["fingerprint"] for k in keys.json()]
        client.post(f"/hosts/{host['id']}/hostkeys/trust", json={"fingerprints": fps})
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 200, r.text
    line = ssh_settings.known_hosts_path.read_text().strip()
    assert line.startswith("spark ssh-ed25519 ")


def test_trusting_a_wrong_fingerprint_changes_nothing(
    ssh_settings: Settings, fake_remote: FakeRemote
) -> None:
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        r = client.post(
            f"/hosts/{host['id']}/hostkeys/trust", json={"fingerprints": ["SHA256:bogus"]}
        )
        assert r.status_code == 502
        assert r.json()["code"] == "hostkey_mismatch"
        assert client.post(f"/hosts/{host['id']}/connect").status_code == 409


def test_sshd_refusing_forwarding_is_explained(
    ssh_settings: Settings, fake_remote: FakeRemote, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import HOSTKEY

    ssh_settings.known_hosts_path.write_text(f"spark ssh-ed25519 {HOSTKEY}\n")
    monkeypatch.setenv("FAKESSH_DENY_FORWARDING", "1")
    with TestClient(create_app(ssh_settings), headers=AUTH) as client:
        host = add_host(client)
        r = client.post(f"/hosts/{host['id']}/connect")
        assert r.status_code == 502
        assert r.json()["code"] == "forwarding_denied"
        assert "AllowStreamLocalForwarding" in r.json()["error"]


def test_scripted_ssh_neutralises_hostile_config_options(tmp_path: Path) -> None:
    spaced = tmp_path / "Application Support" / "newton_known_hosts"
    client = SshClient(SshTarget("spark"), spaced, ssh=sys.executable)

    async def fake_resolve() -> Any:
        from newton_agentd.runners.ssh import ResolvedTarget

        return ResolvedTarget("spark", 22, "tester", ["/etc/ssh/ssh_known_hosts"], proxy=False)

    client.resolve = fake_resolve  # type: ignore[method-assign]
    args = asyncio.run(client.base_args())
    pairs = {args[i + 1] for i, a in enumerate(args) if a == "-o"}
    for opt in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "RemoteCommand=none",
        "RequestTTY=no",
        "ControlMaster=no",
        "ControlPath=none",
        "ForwardAgent=no",
        "UpdateHostKeys=no",
    ):
        assert opt in pairs
    assert f'GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts "{spaced}"' in pairs
    assert not any(p.startswith("UserKnownHostsFile=") for p in pairs)  # user's own stays


def test_ssh_config_discovery(settings: Settings, tmp_path: Path) -> None:
    sync_dir = tmp_path / "Library" / "Application Support" / "NVIDIA" / "Sync" / "config"
    sync_dir.mkdir(parents=True)
    (sync_dir / "ssh_config").write_text(
        "Host SparkLAN\n"
        "    ### CreatedBy: NVIDIA Sync\n"
        "    HostName 192.168.1.42\n"
        "    User alice\n"
        "    Port 22\n"
    )
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    config = ssh_dir / "config"
    config.write_text(
        f'Include "{sync_dir / "ssh_config"}"\n'
        "Host lab-gpu\n    HostName gpu.example.org\n"
        "Host *\n    ServerAliveInterval 30\n"
        "Host weird@alias\n    HostName x\n"
    )
    settings.ssh_config_path = config
    with TestClient(create_app(settings), headers=AUTH) as client:
        hosts = {h["alias"]: h for h in client.get("/ssh/hosts").json()}
        assert set(hosts) == {"SparkLAN", "lab-gpu", "weird@alias"}
        assert hosts["SparkLAN"]["source"] == "nvidia-sync"
        assert hosts["SparkLAN"]["hostname"] == "192.168.1.42"
        assert hosts["SparkLAN"]["host_id"] is None
        assert hosts["weird@alias"]["addable"] is False

        created = client.post("/hosts", json={"name": "spark", "ssh_target": "SparkLAN"}).json()
        hosts = {h["alias"]: h for h in client.get("/ssh/hosts").json()}
        assert hosts["SparkLAN"]["host_id"] == created["id"]


def test_brev_is_gone(client: TestClient) -> None:
    assert client.get("/brev/instances").status_code == 404
    r = client.post("/hosts", json={"name": "b", "ssh_target": "b", "kind": "brev"})
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("typed", "target", "user", "ok"),
    [
        ("alice@spark.local", "spark.local", "alice", True),
        ("[fe80::1]", "fe80::1", None, True),
        ("fe80::1", "fe80::1", None, True),
        ("-oProxyCommand=evil", None, None, False),
        ("spark;rm", None, None, False),
        ("not:ipv6", None, None, False),
    ],
)
def test_typed_host_targets(
    client: TestClient, typed: str, target: str | None, user: str | None, ok: bool
) -> None:
    r = client.post("/hosts", json={"name": f"h-{abs(hash(typed))}", "ssh_target": typed})
    assert (r.status_code == 201) is ok, r.text
    if ok:
        assert r.json()["ssh_target"] == target
        assert r.json()["ssh_user"] == user
