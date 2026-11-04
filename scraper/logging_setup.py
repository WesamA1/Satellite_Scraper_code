"""Phase 1 Task 8: detailed logs to a file, alongside console output."""

import logging
from datetime import datetime, timezone
from pathlib import Path

_configured = False
_log_path: Path | None = None


def configure_logging(log_dir: Path = Path("logs")) -> Path | None:
    """Add a file handler to the root logger, writing to logs/scraper_<UTC>.log.

    Idempotent -- calling this more than once (CLI callback + web app startup,
    say) only configures the handler once. The file is only created once
    something is actually logged, so a command that logs nothing leaves no
    empty file behind. Tests switch this off (tests/conftest.py).
    """
    global _configured, _log_path
    if _configured:
        return _log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _log_path = log_dir / f"scraper_{timestamp}.log"

    handler = logging.FileHandler(_log_path, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    _configured = True
    return _log_path
