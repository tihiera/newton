"""Newton remote worker.

Stdlib-only, Python >= 3.9. Exposes a small HTTP job API on 127.0.0.1 that the
macOS daemon reaches through an SSH tunnel (or directly, for the local runner).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

__version__ = "0.1.0"
# 4: agentd sends each service's API key (X-Newton-Service-Key); admission checks the engine
# 3: model services (/services)  # 2: cpu|cuda|metal backends, Unix-socket transport
PROTOCOL_VERSION = 4


def _read_source_digest() -> Optional[str]:
    path = Path(__file__).resolve().parent.parent / "SOURCE_DIGEST"
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


# Read once at import: a running worker must report the code it actually loaded,
# not whatever an in-progress upgrade has since written to disk.
_SOURCE_DIGEST = _read_source_digest()


def source_digest() -> Optional[str]:
    """Digest of the uploaded worker source this process loaded (written by agentd
    next to the package); agentd compares it to its own copy to upgrade stale
    workers. None when running from a source checkout (local runner, tests)."""
    return _SOURCE_DIGEST
