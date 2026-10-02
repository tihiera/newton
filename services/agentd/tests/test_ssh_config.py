"""ssh_config host discovery (runners/ssh_config.py), against files under tmp_path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from newton_agentd.runners.ssh_config import (
    MAX_INCLUDE_DEPTH,
    ConfigHost,
    list_config_hosts,
)

SYNC_BLOCK = """\
Host SparkLAN
    ### CreatedBy: NVIDIA Sync
    ### UsedBy: NVIDIA Sync
    HostName 192.168.1.42
    IdentityFile "~/Library/Application Support/NVIDIA/Sync/config/nvsync.key"
    Port 22
    User alice
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def aliases(hosts: list[ConfigHost]) -> list[str]:
    return [h.alias for h in hosts]


def by_alias(hosts: list[ConfigHost]) -> dict[str, ConfigHost]:
    return {h.alias: h for h in hosts}


def test_nvidia_sync_quoted_include_with_space(tmp_path: Path) -> None:
    sync = write(
        tmp_path / "Library/Application Support/NVIDIA/Sync/config/ssh_config",
        SYNC_BLOCK + "\nHost SparkTS\n    ### CreatedBy: NVIDIA Sync\n    HostName spark.tailnet\n"
        "    ProxyCommand /usr/bin/nc -x 127.0.0.1:1055 %h %p\n",
    )
    config = write(
        tmp_path / ".ssh/config",
        f'Include "{sync}"\n\nHost other\n    HostName other.example\n',
    )
    hosts = list_config_hosts(config)
    assert hosts == [
        ConfigHost(
            alias="SparkLAN",
            source="nvidia-sync",
            config_file=str(sync),
            hostname="192.168.1.42",
            user="alice",
            port=22,
            proxy=False,
        ),
        ConfigHost(
            alias="SparkTS",
            source="nvidia-sync",
            config_file=str(sync),
            hostname="spark.tailnet",
            user=None,
            port=None,
            proxy=True,
        ),
        ConfigHost(
            alias="other",
            source="ssh-config",
            config_file=str(config),
            hostname="other.example",
            user=None,
            port=None,
            proxy=False,
        ),
    ]


def test_sync_marker_tags_block_outside_sync_dir(tmp_path: Path) -> None:
    config = write(tmp_path / ".ssh/config", SYNC_BLOCK + "Host plain\n    User bob\n")
    hosts = by_alias(list_config_hosts(config))
    assert hosts["SparkLAN"].source == "nvidia-sync"
    assert hosts["plain"].source == "ssh-config"  # the marker does not leak forward


def test_glob_include_sorted(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    write(ssh / "conf.d/b.conf", "Host bee\n")
    write(ssh / "conf.d/a.conf", "Host ay\n")
    write(ssh / "conf.d/c.txt", "Host not-included\n")
    config = write(ssh / "config", "Include conf.d/*.conf\nHost last\n")
    assert aliases(list_config_hosts(config)) == ["ay", "bee", "last"]


def test_relative_include_resolves_against_ssh_dir(tmp_path: Path) -> None:
    ssh_dir = tmp_path / "sshdir"
    write(ssh_dir / "extra", "Host from-ssh-dir\n")
    write(tmp_path / "elsewhere/extra", "Host from-config-dir\n")
    config = write(tmp_path / "elsewhere/config", "Include extra\n")
    assert aliases(list_config_hosts(config, ssh_dir=ssh_dir)) == ["from-ssh-dir"]
    assert aliases(list_config_hosts(config)) == ["from-config-dir"]


def test_tilde_include_and_brev_tagging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    brev = write(tmp_path / ".brev/ssh_config", "Host my-brev-box\n    HostName 10.0.0.9\n")
    config = write(tmp_path / ".ssh/config", "Include ~/.brev/ssh_config\nHost mine\n")
    hosts = by_alias(list_config_hosts(config))
    assert hosts["my-brev-box"].source == "brev"
    assert hosts["my-brev-box"].config_file == str(brev)
    assert hosts["mine"].source == "ssh-config"


def test_missing_includes_are_ignored(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        f"Include {tmp_path}/nope/missing\nInclude nothing-*.conf\nInclude\nHost still\n",
    )
    assert aliases(list_config_hosts(config)) == ["still"]
    assert list_config_hosts(tmp_path / "no-such-config") == []


def test_patterns_are_not_listed(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host *\n    User everyone\n"
        "Host gpu-? *.lan !bastion %h\n"
        'Host good !bad "two words" -oProxyCommand=x\n'
        "    HostName good.example\n",
    )
    hosts = list_config_hosts(config)
    assert aliases(hosts) == ["good"]
    assert hosts[0].user is None  # `Host *` values are not attributed to concrete hosts


def test_match_block_is_skipped(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host alpha\n    HostName alpha.example\n"
        "Match host beta exec true\n    User matchuser\n    Port 2222\n"
        "    ProxyJump hop\n"
        "Host gamma\n",
    )
    hosts = list_config_hosts(config)
    assert aliases(hosts) == ["alpha", "gamma"]
    assert hosts[0] == ConfigHost(
        "alpha", "ssh-config", str(config), "alpha.example", None, None, False
    )


def test_include_inside_match_and_host_blocks(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    write(ssh / "in-match", "Host from-match\n    User m\n")
    write(ssh / "in-host", "Port 2222\nHost beta\n    User b\n")
    config = write(
        ssh / "config",
        "Match all\n    Include in-match\nHost alpha\n    Include in-host\n    User after\n",
    )
    hosts = by_alias(list_config_hosts(config))
    assert list(hosts) == ["from-match", "alpha", "beta"]
    assert hosts["from-match"].user == "m"
    # As OpenSSH does: the included file's leading directives apply to the enclosing
    # block, and after the Include the enclosing block continues.
    assert (hosts["alpha"].user, hosts["alpha"].port) == ("after", 2222)
    assert (hosts["beta"].user, hosts["beta"].port) == ("b", None)


def test_multiple_aliases_share_block(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host one two\tthree\n    HostName shared.example\n    Port 2200\n",
    )
    hosts = list_config_hosts(config)
    assert aliases(hosts) == ["one", "two", "three"]
    assert {(h.hostname, h.port) for h in hosts} == {("shared.example", 2200)}


def test_keyword_syntax_variants(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host=eq-host\n"
        "    HOSTNAME = eq.example\n"
        "\tuser=bob # trailing comment\n"
        "    port  =  2201\n"
        "Host = spaced 'quoted-alias' # comment-alias\n"
        "    HostName first.example\n"
        "    HostName second.example\n"
        '    User "carol"\n',
    )
    hosts = by_alias(list_config_hosts(config))
    assert list(hosts) == ["eq-host", "spaced", "quoted-alias"]
    eq = hosts["eq-host"]
    assert (eq.hostname, eq.user, eq.port) == ("eq.example", "bob", 2201)
    assert hosts["spaced"].hostname == "first.example"  # first value wins
    assert hosts["spaced"].user == "carol"


def test_invalid_ports_and_unterminated_quotes(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host p0\n    Port 0\n    Port 22\n"
        "Host p1\n    Port 70000\n"
        "Host p2\n    Port ssh\n"
        "Host p3\n    Port 65535\n"
        'Host p4\n    HostName "unterminated\n    User u\n'
        'Host p5\n    Port 2200\nHost "broken\n    User leaked\n',
    )
    hosts = by_alias(list_config_hosts(config))
    assert [hosts[a].port for a in ("p0", "p1", "p2", "p3")] == [None, None, None, 65535]
    assert (hosts["p4"].hostname, hosts["p4"].user) == (None, "u")  # bad line skipped
    # A malformed Host line still ends the previous block.
    assert (hosts["p5"].port, hosts["p5"].user) == (2200, None)


def test_dedupe_first_definition_wins(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    write(ssh / "early", "Host dup\n    HostName from-include\n")
    config = write(
        ssh / "config",
        "Include early\nHost dup other\n    HostName from-main\nHost dup\n    User late\n",
    )
    hosts = list_config_hosts(config)
    assert aliases(hosts) == ["dup", "other"]
    assert hosts[0].hostname == "from-include"
    assert hosts[0].user is None
    assert hosts[0].config_file == str(ssh / "early")


def test_proxy_flags(tmp_path: Path) -> None:
    config = write(
        tmp_path / ".ssh/config",
        "Host cmd\n    ProxyCommand ssh -W %h:%p bastion\n"
        "Host jump\n    ProxyJump bastion\n"
        "Host none-first\n    ProxyJump none\n    ProxyCommand nc %h %p\n"
        "Host direct\n    HostName direct.example\n",
    )
    hosts = by_alias(list_config_hosts(config))
    assert {a: h.proxy for a, h in hosts.items()} == {
        "cmd": True,
        "jump": True,
        "none-first": False,
        "direct": False,
    }


def test_include_cycle_terminates(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    write(ssh / "a", "Host in-a\nInclude b\nInclude a\n")
    write(ssh / "b", "Host in-b\nInclude a\nInclude config\nInclude *\n")
    config = write(ssh / "config", "Include a\nHost in-config\nInclude config\n")
    assert aliases(list_config_hosts(config)) == ["in-a", "in-b", "in-config"]


def test_include_depth_limit(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    total = MAX_INCLUDE_DEPTH + 5
    for i in range(1, total):
        write(ssh / f"f{i}", f"Host h{i}\nInclude f{i + 1}\n")
    config = write(ssh / "config", "Host h0\nInclude f1\n")
    assert aliases(list_config_hosts(config)) == [f"h{i}" for i in range(MAX_INCLUDE_DEPTH + 1)]


def test_unreadable_and_special_files_are_skipped(tmp_path: Path) -> None:
    ssh = tmp_path / ".ssh"
    (ssh / "binary").parent.mkdir(parents=True)
    (ssh / "binary").write_bytes(b"Host latin1-\xe9\n")
    write(ssh / "locked", "Host locked\n").chmod(0)
    os.mkfifo(ssh / "fifo")
    (ssh / "adir").mkdir()
    config = write(
        ssh / "config",
        "Include binary\nInclude locked\nInclude fifo\nInclude adir\nInclude /dev/null\nHost ok\n",
    )
    try:
        hosts = list_config_hosts(config)
    finally:
        (ssh / "locked").chmod(0o600)
    expected = ["ok"] if os.geteuid() != 0 else ["locked", "ok"]
    assert aliases(hosts) == expected
