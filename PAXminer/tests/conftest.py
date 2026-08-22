"""Test harness safety: kill the process if RSS grows past a hard cap.

macOS does not support ulimit -v, and RLIMIT_AS breaks numpy/matplotlib's
virtual memory reservations. A sampling watchdog is the portable option.
"""

from __future__ import annotations

import os
import resource
import sys
import threading
import time

# 2 GiB. On Linux ru_maxrss is KiB; on macOS (Darwin) it is bytes.
_RSS_CAP_BYTES = 2 * 1024 * 1024 * 1024
_POLL_SECONDS = 2.0


def _rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(usage)
    return int(usage) * 1024


def _watchdog() -> None:
    while True:
        time.sleep(_POLL_SECONDS)
        if _rss_bytes() > _RSS_CAP_BYTES:
            # Hard exit — a hung MagicMock pagination loop will not respond to
            # soft signals once it is allocating unbounded mock_calls.
            os.write(
                2,
                b"conftest: RSS exceeded 2 GiB; aborting to protect the host\n",
            )
            os._exit(99)


def pytest_configure(config) -> None:  # noqa: ARG001
    t = threading.Thread(target=_watchdog, name="rss-watchdog", daemon=True)
    t.start()
