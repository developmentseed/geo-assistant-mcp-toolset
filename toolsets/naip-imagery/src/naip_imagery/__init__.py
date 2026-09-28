"""naip-imagery toolset.

``.env`` in the working directory is loaded at import (real environment
wins) because that is where the localhost demo keeps its OpenRouter settings
(``OPENROUTER_API_KEY``, ``OPENROUTER_IMAGE_MODEL``) and log level. Logging is
configured here for the same reason and with the same knobs as
duckdb-analyst's ``__init__`` (see its docstring for the full rationale):
``LOG_LEVEL`` falling back to ``FASTMCP_LOG_LEVEL``, default ``INFO``, and
``basicConfig`` no-ops under a host that already configured handlers.
"""

import logging
import os

from dotenv import load_dotenv


def _configure_logging() -> None:
    load_dotenv(".env")
    requested = os.environ.get("LOG_LEVEL") or os.environ.get("FASTMCP_LOG_LEVEL")
    level = (requested or "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if requested:
        logging.getLogger().setLevel(level)


_configure_logging()
