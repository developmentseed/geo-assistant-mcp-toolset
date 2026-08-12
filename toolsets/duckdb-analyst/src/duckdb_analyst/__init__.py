"""duckdb-analyst toolset.

Logging is configured here, at package import, because the interesting part
of this toolset's startup — building and locking down the DuckDB connection —
happens at import time in ``connection.py``, *before* the runtime's FastMCP
server exists. FastMCP configures the root logger itself (from
``FASTMCP_LOG_LEVEL``), but only in ``build_server``, which is too late to
see the connection build. Configuring first from the same environment keeps
one knob for the whole process: ``LOG_LEVEL`` (toolset-specific override)
falling back to ``FASTMCP_LOG_LEVEL`` (which FastMCP and uvicorn also
honor), default ``INFO``. Set either to ``DEBUG`` for verbose logging —
every SQL statement, parameters, timings and row counts.

``.env`` in the working directory is loaded first (real environment wins),
because that is where local dev keeps its configuration — the repo's
``.env`` sets ``LOG_LEVEL=DEBUG`` so local servers are verbose by default —
and because FastMCP reads its own ``FASTMCP_*`` settings from the same file:
without loading it here too, a level set only in ``.env`` would never reach
this earlier, import-time configuration. Deployed pods carry no ``.env``, so
production stays at INFO unless the env var is set (see ``toolset.yaml``).

``logging.basicConfig`` is a no-op when the root logger already has handlers
(e.g. under pytest or an embedding application), so this never clobbers a
host process's logging setup — and FastMCP's own later ``basicConfig`` call
is likewise a no-op once this has run.
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
        # basicConfig no-ops when handlers already exist; an explicitly
        # requested level should still apply to whatever handler is installed.
        logging.getLogger().setLevel(level)


_configure_logging()
