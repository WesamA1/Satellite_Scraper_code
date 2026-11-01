"""Phase 1 Task 8: detailed logs to a file, alongside console output."""

import logging
from datetime import datetime, timezone
from pathlib import Path

_configured = False


def configure_logging(log_dir: Path = Path("logs")) -> Path:
    """Add a file handler to the root logger, writing to logs/scraper_<UTC>.log.

    Idempotent -- calling this more than once (CLI callback + web app startup,
    say) only configures the handler once.
    """
    global _configured
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"scraper_{timestamp}.log"

    if not _configured:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)
        _configured = True

    return log_path
