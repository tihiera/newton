"""Wires the daemon's services together."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .config import Settings
from .connectors.publish import Publisher
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
    started_at: float = field(default_factory=time.time)

    @classmethod
    def create(cls, settings: Settings, secret_store: SecretStore | None = None) -> AppContext:
        settings.ensure_dirs()
        settings.resolve_api_token()
        db = Database(settings.db_path)
        db.migrate()
        store = secret_store or make_secret_store(settings.secret_backend, settings.data_dir)
        hosts = HostService(settings, db, store)
        hosts.ensure_local_host()
        approvals = Approvals(db)
        experiments = Experiments(settings, db, approvals)
        profile = Profile(db, store)
        services = ServiceManager(settings, db, hosts, approvals, store, profile)
        router = Router(settings, db, services, profile, store)
        scheduler = Scheduler(settings, db, hosts, router)
        papers = Papers(settings, db, router, profile)
        loop = ResearchLoop(settings, db, papers, experiments, router, profile)
        publisher = Publisher(settings, db, approvals, store)
        return cls(settings, db, store, hosts, approvals, experiments, scheduler, services,
                   profile, router, papers, loop, publisher)  # fmt: skip

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

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.publisher.close()
        await self.loop.close()
        await self.papers.close()
        await self.router.close()
        await self.services.stop_loop()
        await self.hosts.close_all()
        self.db.close()
