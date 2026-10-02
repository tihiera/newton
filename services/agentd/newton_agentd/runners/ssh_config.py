"""Host discovery from the user's OpenSSH client config, for a "pick a host" list.

- Parses ~/.ssh/config and everything it Includes following ssh_config(5):
  case-insensitive keywords, `Keyword value` or `Keyword=value`, quoted arguments,
  globbed / `~` / relative (to ~/.ssh) Include paths, first value wins per block.
- Lists only concrete aliases (what `ssh <alias>` accepts). Wildcard, negated and
  token patterns are skipped; Match blocks are skipped entirely.
- Hosts written by NVIDIA Sync (DGX Spark pairing) are tagged "nvidia-sync" and the
  aliases `brev refresh` writes are tagged "brev", so the UI can group or hide them.
- Read-only parsing: nothing is executed, unreadable files are skipped, never raises.
"""

from __future__ import annotations

import glob
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_INCLUDE_DEPTH = 16  # same limit as OpenSSH's READCONF_MAX_DEPTH
MAX_FILE_BYTES = 4 * 1024 * 1024

SOURCE_NVIDIA_SYNC = "nvidia-sync"
SOURCE_BREV = "brev"
SOURCE_SSH_CONFIG = "ssh-config"

# Keyword ends at whitespace or a single '=' (optionally surrounded by whitespace).
_KEYWORD_RE = re.compile(r'^[ \t]*([^ \t="]+)[ \t]*(?:=[ \t]*)?(.*)$')
_SYNC_MARKER_RE = re.compile(r"CreatedBy:\s*NVIDIA\s+Sync", re.IGNORECASE)
_PORT_RE = re.compile(r"^[0-9]{1,5}$")
_PATTERN_CHARS = frozenset("*?!%")
_TRACKED = frozenset({"hostname", "user", "port", "proxy"})


@dataclass(frozen=True)
class ConfigHost:
    alias: str  # the Host pattern to pass to `ssh <alias>`
    source: str  # "nvidia-sync" | "brev" | "ssh-config"
    config_file: str  # file the Host block was found in
    hostname: str | None  # HostName from that block, if any (informational)
    user: str | None
    port: int | None
    proxy: bool  # block has ProxyCommand or ProxyJump (other than "none")


@dataclass
class _Block:
    aliases: list[str]
    config_file: str
    nvidia_marker: bool = False
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    proxy: bool = False
    seen: set[str] = field(default_factory=set)

    def apply(self, keyword: str, value: str) -> None:
        if keyword in ("proxycommand", "proxyjump"):
            keyword = "proxy"  # whichever of the two comes first wins, as in OpenSSH
        if keyword not in _TRACKED or keyword in self.seen:
            return
        self.seen.add(keyword)
        if keyword == "hostname":
            self.hostname = value
        elif keyword == "user":
            self.user = value
        elif keyword == "port":
            self.port = _parse_port(value)
        else:
            self.proxy = value.lower() != "none"

    @property
    def source(self) -> str:
        if self.nvidia_marker or "/NVIDIA/Sync/" in self.config_file:
            return SOURCE_NVIDIA_SYNC
        if "/.brev/" in self.config_file:
            return SOURCE_BREV
        return SOURCE_SSH_CONFIG


def list_config_hosts(config_path: Path, ssh_dir: Path | None = None) -> list[ConfigHost]:
    """Concrete Host aliases in `config_path` and its Includes, in file order.

    Relative Include paths resolve against `ssh_dir` (default: the config's directory).
    An alias defined more than once keeps its first definition.
    """
    parser = _Parser(ssh_dir if ssh_dir is not None else config_path.parent)
    parser.parse_path(config_path, None, 0)
    seen: set[str] = set()
    out: list[ConfigHost] = []
    for block in parser.blocks:
        for alias in block.aliases:
            if alias in seen:
                continue
            seen.add(alias)
            out.append(
                ConfigHost(
                    alias=alias,
                    source=block.source,
                    config_file=block.config_file,
                    hostname=block.hostname,
                    user=block.user,
                    port=block.port,
                    proxy=block.proxy,
                )
            )
    return out


class _Parser:
    def __init__(self, ssh_dir: Path) -> None:
        self.ssh_dir = ssh_dir
        self.blocks: list[_Block] = []
        self.visited: set[str] = set()  # each file is read at most once (cycles, fan-out)

    def parse_path(self, path: Path, block: _Block | None, depth: int) -> None:
        if depth > MAX_INCLUDE_DEPTH:
            return
        try:
            key = os.path.realpath(path)
        except (OSError, ValueError):
            return
        if key in self.visited:
            return
        self.visited.add(key)
        text = _read_text(path)
        if text is not None:
            self._parse_text(text, str(path), block, depth)

    def _parse_text(self, text: str, filename: str, block: _Block | None, depth: int) -> None:
        # `block` is the enclosing Host block when this file is Included from inside one:
        # directives before this file's first Host/Match line belong to it (as in OpenSSH).
        # The caller's block is unaffected by Host lines here, since `block` is local.
        for raw in text.split("\n"):
            line = raw.rstrip(" \t\r\n\f")
            stripped = line.lstrip(" \t")
            if not stripped:
                continue
            if stripped.startswith("#"):
                if block is not None and _SYNC_MARKER_RE.search(stripped):
                    block.nvidia_marker = True
                continue
            m = _KEYWORD_RE.match(line)
            if m is None:
                continue
            keyword = m.group(1).lower()
            args = _split_args(m.group(2))
            if keyword in ("host", "match"):
                block = None
                if keyword == "host" and args:
                    block = _Block(aliases=_concrete_aliases(args), config_file=filename)
                    if block.aliases:
                        self.blocks.append(block)
                continue
            if not args:
                continue
            if keyword == "include":
                for arg in args:
                    for path in _expand_include(arg, self.ssh_dir):
                        self.parse_path(path, block, depth + 1)
            elif block is not None:
                block.apply(keyword, args[0])


def _split_args(s: str) -> list[str] | None:
    """OpenSSH argv_split(): whitespace-separated, '…' or "…" quoting, a few
    backslash escapes, `#` at the start of an argument ends the line.
    Returns None on an unterminated quote (OpenSSH rejects such lines)."""
    args: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] in " \t":
            i += 1
            continue
        if s[i] == "#":
            break
        quote = ""
        buf: list[str] = []
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n and (s[i + 1] in "'\"\\" or (not quote and s[i + 1] == " ")):
                buf.append(s[i + 1])
                i += 2
                continue
            if not quote and c in " \t":
                break
            if not quote and c in "'\"":
                quote = c
            elif quote and c == quote:
                quote = ""
            else:
                buf.append(c)
            i += 1
        if quote:
            return None
        args.append("".join(buf))
    return args


def _concrete_aliases(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        if not p or p.startswith("-"):  # a leading '-' would read as an ssh option
            continue
        if any(c in _PATTERN_CHARS or c.isspace() for c in p):
            continue
        out.append(p)
    return out


def _parse_port(value: str) -> int | None:
    if not _PORT_RE.match(value):
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def _expand_include(arg: str, ssh_dir: Path) -> list[Path]:
    pattern = os.path.expanduser(arg)
    if pattern.startswith("~"):  # unknown user
        return []
    if not os.path.isabs(pattern):
        pattern = os.path.join(ssh_dir, pattern)
    try:
        matches = glob.glob(pattern)
    except (OSError, ValueError):
        return []
    return [Path(p) for p in sorted(matches)]


def _read_text(path: Path) -> str | None:
    # O_NONBLOCK so a FIFO cannot block the open; only regular files are read.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
