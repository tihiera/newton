"""Fake `ssh` / `ssh-keyscan` for tests. The "remote" is a local directory.

Environment:
    FAKESSH_REMOTE_HOME       HOME for remote commands (the fake Linux box)
    FAKESSH_HOSTKEY           base64 ed25519 key the fake host presents
    FAKESSH_REMOTE_PATH       PATH for remote commands (default: inherited)
    FAKESSH_LOGIN_SHELL       the remote user's login shell (default: sh), e.g. /bin/csh
    FAKESSH_USER_KNOWN_HOSTS  UserKnownHostsFile from the user's ssh config (one path,
                              may contain spaces, like NVIDIA Sync's Application Support)
    FAKESSH_PROXY             if set, the alias has a ProxyCommand: ssh-keyscan can't
                              reach it (only ssh, which runs the proxy, can)
    FAKESSH_FORWARD_DROP_FILE while this file exists, forwarded connections are dropped
    FAKESSH_DENY_FORWARDING   if set, sshd refuses stream-local forwarding (reported, as by
                              real OpenSSH, only as "connect failed: open failed")

Behaviour mirrors the subset of OpenSSH that Newton uses:
    ssh -G target                       print resolved config
    ssh [opts] target -- <command>      run <command> via the login shell in the remote home
    ssh [opts] -N -L l:h:r target       forward 127.0.0.1:l → 127.0.0.1:r until killed
    ssh [opts] -N -L l:/path target     forward 127.0.0.1:l → Unix socket /path
    ssh -o ControlMaster=yes -o ControlPath=cm -N target   master on control socket cm
    ssh -S cm -O check|exit target      mux client
    ssh -S cm -O forward -L /local.sock:/remote.sock target   add a stream-local forward
    ssh -S cm -O forward -L 127.0.0.1:l:127.0.0.1:r target     add a TCP forward
    ssh-keyscan [-T n] [-p n] host      print the host key
Host keys are enforced like StrictHostKeyChecking=yes: the key line must be in a
UserKnownHostsFile or GlobalKnownHostsFile file (multi-path values honour double
quotes, like OpenSSH), or be printed by KnownHostsCommand; otherwise exit 255.
"""

from __future__ import annotations

import json
import os
import selectors
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

OPTS_WITH_ARG = {"-o", "-p", "-l", "-L", "-F", "-i", "-J", "-T", "-S", "-O"}
DEFAULT_GLOBAL = "/etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2"


def parse(argv: list[str]) -> tuple[dict[str, list[str]], list[str], list[str]]:
    opts: dict[str, list[str]] = {}
    flags: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if rest:
            rest.append(a)
        elif a in OPTS_WITH_ARG:
            if i + 1 >= len(argv) or (a == "-o" and argv[i + 1].startswith("-")):
                sys.stderr.write(f'command-line line 0: no argument after keyword "{a}"\n')
                sys.exit(255)
            opts.setdefault(a, []).append(argv[i + 1])
            i += 1
        elif a.startswith("-") and a != "--":
            flags.append(a)
        elif a != "--":
            rest.append(a)
        i += 1
    rest = [r for r in rest if r != "--"] if rest else rest
    return opts, flags, rest


def option(opts: dict[str, list[str]], name: str) -> str | None:
    for value in opts.get("-o", []):  # first value wins, as in OpenSSH
        key, _, v = value.partition("=")
        if key.lower() == name.lower():
            return v
    return None


def host_key_line(host: str, port: int) -> str:
    name = host if port == 22 else f"[{host}]:{port}"
    return f"{name} ssh-ed25519 {os.environ['FAKESSH_HOSTKEY']}"


def known_hosts_files(opts: dict[str, list[str]]) -> list[str]:
    user = option(opts, "UserKnownHostsFile")
    users = shlex.split(user) if user is not None else []
    if user is None and os.environ.get("FAKESSH_USER_KNOWN_HOSTS"):
        users = [os.environ["FAKESSH_USER_KNOWN_HOSTS"]]  # from the user's config
    glob = option(opts, "GlobalKnownHostsFile")
    return users + shlex.split(glob if glob is not None else DEFAULT_GLOBAL)


def check_host_key(opts: dict[str, list[str]], host: str, port: int) -> None:
    want = host_key_line(host, port)
    for f in known_hosts_files(opts):
        p = Path(os.path.expanduser(f))
        if p.is_file() and want in p.read_text().splitlines():
            return
    command = option(opts, "KnownHostsCommand")
    if command:
        name, key_type, key = want.split(" ")
        # Like real ssh: first an ORDER call with no key, then the HOSTNAME lookup.
        for reason, t, k in (("ORDER", "NONE", "NONE"), ("HOSTNAME", key_type, key)):
            argv = [
                a.replace("%I", reason).replace("%H", name).replace("%t", t).replace("%K", k)
                for a in shlex.split(command)
            ]
            out = subprocess.run(argv, capture_output=True, text=True).stdout
            if reason == "HOSTNAME" and want in out.splitlines():
                return
    sys.stderr.write(
        f"No ED25519 host key is known for {host} and you have requested strict checking.\n"
        "Host key verification failed.\n"
    )
    sys.exit(255)


def pipe(a: socket.socket, b: socket.socket) -> None:
    sel = selectors.DefaultSelector()
    sel.register(a, selectors.EVENT_READ, b)
    sel.register(b, selectors.EVENT_READ, a)
    try:
        while True:
            for key, _ in sel.select():
                data = key.fileobj.recv(65536)  # type: ignore[union-attr]
                if not data:
                    return
                key.data.sendall(data)
    except OSError:
        return
    finally:
        a.close()
        b.close()


def listen(local_port: int) -> socket.socket:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", local_port))
    srv.listen(64)
    return srv


def forward(local_port: int, target: tuple[str, int] | str) -> None:
    serve_forward(listen(local_port), target)


def serve_forward(srv: socket.socket, target: tuple[str, int] | str) -> None:
    while True:
        client, _ = srv.accept()
        drop = os.environ.get("FAKESSH_FORWARD_DROP_FILE")
        if drop and os.path.exists(drop):  # tests: the master lives, the forward is broken
            client.close()
            continue
        if os.environ.get("FAKESSH_DENY_FORWARDING"):
            sys.stderr.write("channel 2: open failed: administratively prohibited: open failed\n")
            sys.stderr.flush()
            client.close()
            continue
        try:
            if isinstance(target, str):
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(target)
            else:
                upstream = socket.create_connection(target)
        except OSError:
            # like ssh: "channel open failed: connect failed", connection dropped
            sys.stderr.write("channel 3: open failed: connect failed: No such file\n")
            sys.stderr.flush()
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()


def deny_or_connect(target: str) -> socket.socket:
    if os.environ.get("FAKESSH_DENY_FORWARDING"):
        raise OSError("refused streamlocal port forward")
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.connect(target)
    return upstream


def serve_unix_forward(local: str, remote: str) -> None:
    if os.path.exists(local):
        os.unlink(local)  # StreamLocalBindUnlink=yes
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(local)
    os.chmod(local, 0o600)  # StreamLocalBindMask=0177
    srv.listen(64)
    while True:
        client, _ = srv.accept()
        try:
            upstream = deny_or_connect(remote)
        except OSError:
            # Real OpenSSH says this for a missing socket *and* for a refused forward.
            sys.stderr.write("channel 3: open failed: connect failed: open failed\n")
            sys.stderr.flush()
            client.close()
            continue
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()


def run_master(control: str) -> None:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(control)
    srv.listen(16)
    srv.settimeout(0.5)
    while True:
        if os.getppid() == 1:
            os._exit(0)  # our agentd is gone: never outlive it
        try:
            conn, _ = srv.accept()
        except TimeoutError:
            continue
        request = json.loads(conn.makefile().readline() or "{}")
        if request.get("op") == "forward":
            parts = request["spec"].split(":")
            if len(parts) == 4:  # 127.0.0.1:local:127.0.0.1:remote (TCP)
                target = (parts[2], int(parts[3]))
                try:  # bind before answering: like ssh, a taken port fails the request
                    listener = listen(int(parts[1]))
                except OSError:
                    conn.sendall(b'{"ok": false}\n')
                    conn.close()
                    continue
                threading.Thread(target=serve_forward, args=(listener, target),
                                 daemon=True).start()  # fmt: skip
            else:
                local, remote = request["spec"].split(":", 1)
                threading.Thread(target=serve_unix_forward, args=(local, remote),
                                 daemon=True).start()  # fmt: skip
            time.sleep(0.05)
        conn.sendall(b'{"ok": true}\n')
        conn.close()
        if request.get("op") == "exit":
            os._exit(0)


def mux_client(control: str, op: str, opts: dict[str, list[str]]) -> None:
    request = {"op": op}
    if op == "forward":
        request["spec"] = opts["-L"][0]
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(control)
    except OSError:
        sys.stderr.write(f"Control socket connect({control}): No such file or directory\n")
        sys.exit(255)
    conn.sendall((json.dumps(request) + "\n").encode())
    reply = json.loads(conn.makefile().readline() or "{}")
    sys.exit(0 if reply.get("ok") else 255)


def ssh(argv: list[str]) -> None:
    opts, flags, rest = parse(argv)
    if "-O" in opts:
        mux_client(opts["-S"][0], opts["-O"][0], opts)
    if "-G" in flags:
        target = rest[0]
        port = opts.get("-p", ["22"])[0]
        user_known = os.environ.get("FAKESSH_USER_KNOWN_HOSTS", "~/.ssh/known_hosts")
        lines = [
            f"hostname {target}",
            f"port {port}",
            "user tester",
            f"userknownhostsfile {user_known}",
            f"globalknownhostsfile {DEFAULT_GLOBAL}",
        ]
        if os.environ.get("FAKESSH_PROXY"):
            lines.append("proxycommand /opt/fake/proxy %h %p")
        print("\n".join(lines))
        return
    target, command = rest[0], rest[1:]
    port = int(opts.get("-p", ["22"])[0])
    check_host_key(opts, target, port)
    if "-N" in flags and (option(opts, "ControlMaster") or "").lower() == "yes":
        run_master(option(opts, "ControlPath") or "")
        return
    if "-N" in flags:
        spec = opts["-L"][0].split(":")
        if len(spec) == 3:  # 127.0.0.1:lport:/remote/socket
            forward(int(spec[1]), spec[2])
        else:  # 127.0.0.1:lport:host:rport
            forward(int(spec[1]), (spec[2], int(spec[3])))
        return
    home = os.environ["FAKESSH_REMOTE_HOME"]
    env = {**os.environ, "HOME": home}
    env.pop("NEWTON_HOME", None)
    if os.environ.get("FAKESSH_REMOTE_PATH"):
        env["PATH"] = os.environ["FAKESSH_REMOTE_PATH"]
    login_shell = os.environ.get("FAKESSH_LOGIN_SHELL", "sh")
    rc = subprocess.run([login_shell, "-c", " ".join(command)], cwd=home, env=env).returncode
    sys.exit(rc)


def keyscan(argv: list[str]) -> None:
    opts, _, rest = parse(argv)
    if os.environ.get("FAKESSH_PROXY"):
        sys.stderr.write(f"{rest[0]}: Name or service not known\n")
        sys.exit(1)
    port = int(opts.get("-p", ["22"])[0])
    print(host_key_line(rest[0], port))


if __name__ == "__main__":
    mode = sys.argv[1]
    (ssh if mode == "ssh" else keyscan)(sys.argv[2:])
