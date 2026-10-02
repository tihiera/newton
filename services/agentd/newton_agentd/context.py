"""Wires the daemon's services together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .connectors.publish import Publisher
from .network import NetworkState
from .orchestration.approvals import Approvals
from .orchestration.hosts import HostService
from .orchestration.scheduler import Scheduler
from .profile import Profile
from .research.experiment_design import Experiments
from .research.loop import ResearchLoop
from .research.papers import Papers
from .secrets import SecretStore, make_secret_store
from .serving.manager import ServiceManager
from .serving.router import Router
from .storage.db import Database

log = logging.getLogger("newton_agentd")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    secrets: SecretStore
    hosts: HostService
    approvals: Approvals
    experiments: Experiments
    scheduler: Scheduler
    services: ServiceManager
    profile: Profile
    router: Router
    papers: Papers
    loop: ResearchLoop
    publisher: Publisher
    network: NetworkState = field(default_factory=NetworkState)
    started_at: float = field(default_factory=time.time)
    _local_check: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, settings: Settings, secret_store: SecretStore | None = None) -> AppContext:
        settings.ensure_dirs()
        settings.resolve_api_token()
        db = Database(settings.db_path)
        db.migrate()
        store = secret_store or make_secret_store(settings.secret_backend, settings.data_dir)
        network = NetworkState()  # what Newton's own calls show: online or not
        hosts = HostService(settings, db, store)
        hosts.ensure_local_host()
        approvals = Approvals(db)
        experiments = Experiments(settings, db, approvals)
        profile = Profile(db, store)
        services = ServiceManager(settings, db, hosts, approvals, store, profile)
        router = Router(settings, db, services, profile, store)
        scheduler = Scheduler(settings, db, hosts, router)
        papers = Papers(settings, db, router, profile, network)
        loop = ResearchLoop(settings, db, papers, experiments, router, profile)
        publisher = Publisher(settings, db, approvals, store, network)
        return cls(settings, db, store, hosts, approvals, experiments, scheduler, services,
                   profile, router, papers, loop, publisher, network)  # fmt: skip

    async def start(self) -> None:
        try:  # first use creates the router key: not on a request
            await asyncio.to_thread(self.profile.router_key)
        except Exception:
            log.warning("router key unavailable (is the keychain locked?)", exc_info=True)
        if self.settings.start_scheduler:
            # Before /v1 serves anything: a timed job still running keeps its host.
            self.scheduler.retain_leases()
            self.scheduler.start()
            self.services.start()
            self.router.start()
            self.loop.start()
            # This Mac's capabilities (CPU, Metal) are known from the first second, not
            # only after its first experiment or a manual Check.
            self._local_check = asyncio.create_task(self._check_local())

    async def _check_local(self) -> None:
        try:
            await self.hosts.ensure_checked("local")
        except Exception:  # shown on the host card (last_error); never fatal at start
            log.warning("checking this Mac at start failed", exc_info=True)

    async def stop(self) -> None:
        task = self._local_check
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.scheduler.stop()
        await self.publisher.close()
        await self.loop.close()
        await self.papers.close()
        await self.router.close()
        await self.services.stop_loop()
        await self.hosts.close_all()
        self.db.close()
