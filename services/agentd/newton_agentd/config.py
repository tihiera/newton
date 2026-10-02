"""Daemon settings, resolved from environment variables with sane macOS defaults."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .connectors.oauth_apps import (
    GITHUB_CLIENT_ID,
    NOTION_BROKER_URL,
    NOTION_CLIENT_ID,
    NOTION_REDIRECT_URI,
)


def _default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Newton"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "newton"


def _repo_root() -> Path:
    # services/agentd/newton_agentd/config.py → repo root
    return Path(__file__).resolve().parents[3]


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: _env_path("NEWTON_DATA_DIR", _default_data_dir())
    )
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: int(os.environ.get("NEWTON_PORT", "8765")))
    api_token: str | None = field(default_factory=lambda: os.environ.get("NEWTON_API_TOKEN"))
    # keychain (macOS Keychain via `keyring`), memory (tests), file (dev on non-macOS)
    secret_backend: str = field(
        default_factory=lambda: os.environ.get("NEWTON_SECRET_BACKEND", "keychain")
    )
    resources_dir: Path = field(
        default_factory=lambda: _env_path("NEWTON_RESOURCES_DIR", _repo_root())
    )
    ssh_executable: str = field(default_factory=lambda: os.environ.get("NEWTON_SSH", "ssh"))
    ssh_keyscan_executable: str = field(
        default_factory=lambda: os.environ.get("NEWTON_SSH_KEYSCAN", "ssh-keyscan")
    )
    # Kept out of data_dir: ssh's option parser splits on the space in "Application Support".
    known_hosts_path: Path = field(
        default_factory=lambda: _env_path(
            "NEWTON_KNOWN_HOSTS", Path.home() / ".ssh" / "newton_known_hosts"
        )
    )
    ssh_config_path: Path = field(
        default_factory=lambda: _env_path("NEWTON_SSH_CONFIG", Path.home() / ".ssh" / "config")
    )
    scheduler_interval: float = field(
        default_factory=lambda: float(os.environ.get("NEWTON_SCHEDULER_INTERVAL", "1.0"))
    )
    # The router (SV3): how long a request may wait for a free slot, how many may wait
    # per model, how long an engine may take between two bytes of its answer, and how
    # long a timed benchmark waits for in-flight requests on its host before aborting them.
    router_queue_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEWTON_ROUTER_QUEUE_TIMEOUT", "300"))
    )
    router_max_queue: int = field(
        default_factory=lambda: int(os.environ.get("NEWTON_ROUTER_MAX_QUEUE", "256"))
    )
    router_read_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEWTON_ROUTER_READ_TIMEOUT", "600"))
    )
    lease_drain_timeout: float = field(
        default_factory=lambda: float(os.environ.get("NEWTON_LEASE_DRAIN_TIMEOUT", "120"))
    )
    # The router's log (timings and counts only, never content): kept this long / this big.
    router_log_days: float = field(
        default_factory=lambda: float(os.environ.get("NEWTON_ROUTER_LOG_DAYS", "30"))
    )
    router_log_rows: int = field(
        default_factory=lambda: int(os.environ.get("NEWTON_ROUTER_LOG_ROWS", "200000"))
    )
    # One-click Connect (connectors/oauth_apps.py has the defaults: public values only).
    github_client_id: str = field(
        default_factory=lambda: os.environ.get("NEWTON_GITHUB_CLIENT_ID", GITHUB_CLIENT_ID)
    )
    notion_client_id: str = field(
        default_factory=lambda: os.environ.get("NEWTON_NOTION_CLIENT_ID", NOTION_CLIENT_ID)
    )
    notion_broker_url: str = field(
        default_factory=lambda: os.environ.get("NEWTON_NOTION_BROKER_URL", NOTION_BROKER_URL)
    )
    # Notion matches redirect URIs exactly (oauth_apps.NOTION_REDIRECT_URI is the one
    # registered with the integration); empty: notion_callback_url's default, on
    # 127.0.0.1 (the address agentd binds), never "localhost": that may resolve to ::1
    # first, where another local process could listen on the same port and take the code.
    notion_redirect_uri: str = field(
        default_factory=lambda: os.environ.get("NEWTON_NOTION_REDIRECT_URI", NOTION_REDIRECT_URI)
    )
    # Base URLs, overridden only for local end-to-end tests.
    github_web: str = field(
        default_factory=lambda: os.environ.get("NEWTON_GITHUB_WEB", "https://github.com")
    )
    github_api: str = field(
        default_factory=lambda: os.environ.get("NEWTON_GITHUB_API", "https://api.github.com")
    )
    notion_api: str = field(
        default_factory=lambda: os.environ.get("NEWTON_NOTION_API", "https://api.notion.com/v1")
    )
    start_scheduler: bool = True
    max_submit_attempts: int = 3

    @property
    def notion_callback_url(self) -> str:
        return (self.notion_redirect_uri
                or f"http://127.0.0.1:{self.port}/connectors/notion/callback")  # fmt: skip

    @property
    def db_path(self) -> Path:
        return self.data_dir / "newton.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def local_worker_root(self) -> Path:
        return self.data_dir / "local-worker"

    @property
    def benchmarks_dir(self) -> Path:
        return self.resources_dir / "benchmarks"

    @property
    def worker_source_dir(self) -> Path:
        return self.resources_dir / "services" / "worker"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.artifacts_dir, self.reports_dir, self.local_worker_root):
            d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)

    def resolve_api_token(self) -> str:
        """Return the API token, creating a 0600 token file on first start."""
        if self.api_token:
            return self.api_token
        path = self.data_dir / "api-token"
        if path.exists():
            token = path.read_text().strip()
            if token:
                self.api_token = token
                return token
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        self.api_token = token
        return token
