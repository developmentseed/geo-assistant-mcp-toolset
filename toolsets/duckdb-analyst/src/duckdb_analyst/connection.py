"""The one DuckDB connection this toolset ever opens, and its security model.

Read this before touching ``tools.py``. The connection is built once, at
import time, in :func:`_build_connection`: extensions are installed, the
curated views are created, and then the connection is locked down before any
caller-supplied SQL ever runs against it. ``query``/``chart`` in ``tools.py``
only ever get a ``cursor()`` off the already-locked-down :data:`CON`.

SECURITY MODEL — four layers, in decreasing order of how much they actually
protect this connection from a hostile ``SELECT``:

1. **The deployment has no local secrets reachable, full stop.** This is the
   PRIMARY control, and it lives outside this file entirely: nothing in
   Python can compensate for a `.env`, a mounted Secret, or an ambient AWS
   credential sitting in this container's filesystem or environment. This
   toolset's ``pyproject.toml`` declares no ``CREDENTIAL_HEADERS``, needs no
   secrets in ``toolset.yaml``, and the shared ``Dockerfile`` ships a bare
   ``python:3.12-slim-bookworm`` runtime image with nothing baked in beyond
   the venv. If that ever changes for this toolset specifically, this
   connection's threat model changes with it — grep for "PRIMARY control"
   before adding any credential to this toolset's deployment.

2. **DuckDB is configured so its own filesystem access can't reach local
   disk while ``https://``/``s3://`` reads keep working.** DuckDB's
   documented, all-or-nothing knob (``SET enable_external_access = false``)
   was tried first and rejected: it disables ``read_csv``/``read_parquet``/
   ``read_json`` reading from *any* external source, remote included — see
   https://duckdb.org/docs/stable/operations_manual/securing_duckdb/overview.
   ``SET disabled_filesystems = 'LocalFileSystem'`` is DuckDB's documented,
   finer-grained alternative (it's literally their own worked example for
   blocking ``read_csv('/etc/passwd', ...)``), and empirically it *does*
   deliver local-blocked/remote-allowed — but only once the connection's
   remote filesystem path has been exercised at least once beforehand. Prior
   to that first remote read, DuckDB's own httpfs/spatial code paths still
   touch local disk internally (e.g. to resolve/cache a filesystem handle),
   so a `disabled_filesystems` set on a "cold" connection blocks the first
   remote read too — a real DuckDB limitation, tracked upstream as
   https://github.com/duckdb/duckdb/issues/15734 for the plain-httpfs case,
   and reproduced here for ``spatial``'s ``ST_Read`` as well. Verified
   directly against this toolset's pinned DuckDB version (see the
   ``dependencies`` pin in ``pyproject.toml``): one warmup read of *any*
   remote URL, of *any* kind (plain https, s3://, or ``ST_Read``), before
   locking down, is sufficient — every remote kind then keeps working for
   arbitrary new URLs afterwards, while local paths stay blocked. That is
   exactly what ``_warm_up_remote_filesystem`` below does, and it runs
   before ``_register_views`` for good measure (creating each view already
   touches its remote source once, but the explicit warmup makes the
   ordering requirement self-documenting instead of incidental). This is a
   real, verified technical control, not a fig leaf — but it rests on an
   *undocumented* DuckDB behavior (one-time-touch-then-cached), which is one
   more reason layer 1 is the control this toolset actually depends on, not
   this one.
3. ``SET lock_configuration = true`` is set last, after every setting above,
   so no runtime SQL — even something that slips past ``security.py``'s
   statement-shape filter — can loosen any of it. ``SET
   allow_community_extensions = false`` runs first, before any ``INSTALL``:
   this toolset needs only core extensions (``httpfs``, ``spatial``), so
   community extensions are refused outright rather than allowed once and
   then closed off.
4. ``security.validate_select_only`` (defense-in-depth, checked before any
   caller SQL reaches this connection): single-statement, ``SELECT``/``WITH``
   only, plus a small denylist for introspection functions
   (``duckdb_secrets()`` etc.) that are otherwise valid inside a plain
   ``SELECT``. This does **not** stop ``read_text('/etc/passwd')`` — that
   starts with ``SELECT`` and is one statement, so it sails through this
   check. It's stopped by layer 2 (and, ultimately, guaranteed by layer 1).

Extensions are pinned by name (``httpfs``, ``spatial``) — nothing under
``duckdb-analyst`` ever calls ``INSTALL``/``LOAD`` again after connection
setup, and layer 3 makes sure caller SQL couldn't anyway. Both are core
DuckDB extensions, so their versions are pinned transitively by this
toolset's pinned ``duckdb`` dependency. No community extension is used, so
there is no unpinnable dependency here.

SCOPE — what this toolset is for:

Small, fast datasets read over the network, plus ad hoc ``https://``/
``s3://`` parquet and CSV URLs. The curated views are deliberately
lightweight (Natural Earth 1:110m, a few hundred KB each), so a full
``GROUP BY`` over one of them returns in well under a second.

Large remote datasets are out of scope, and that is a measured limit rather
than an untested guess. Overture Maps was tried here first and removed: its
themes are hundreds of GB, the public parquet layout gives no partition
pruning for the fields an analyst actually filters on (``country``,
``locality``), and so any aggregate query scans the whole theme. Measured
against ``_MEMORY_LIMIT``/``_THREADS`` below and the 30s watchdog in
``security.py``: a bare ``SELECT ... LIMIT 3`` took ~2.8s, adding ``WHERE
country = 'PT'`` took ~17.4s, and a ``GROUP BY`` exceeded the timeout
outright. A dataset that size wants a local copy (this is the same
conclusion ``gazet`` reached, which is why it ships local Overture
extracts), not a remote scan — and a local copy is just another
``CREATE VIEW`` here, needing no change to ``tools.py`` or the security
model.
"""

import asyncio
import logging
import os
import threading
import time
from typing import Any, NotRequired, TypedDict

import duckdb

from duckdb_analyst.security import LOOKUP_TIMEOUT_SECONDS, QUERY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


def _oneline(sql: str, max_length: int = 2000) -> str:
    """Collapse SQL to one whitespace-normalized line for a log record."""
    flat = " ".join(sql.split())
    return flat if len(flat) <= max_length else flat[:max_length] + "…"


# Official Natural Earth vector data, unzipped, served directly over plain
# https by the project's own GitHub organization
# (https://github.com/nvkelso/natural-earth-vector — nvkelso is a Natural
# Earth maintainer; this is the canonical vector-data repo, not a random
# mirror). Deliberately NOT the zipped shapefiles naturalearthdata.com links
# to: this DuckDB build's `spatial` extension has no working GDAL vsicurl
# support (verified: `/vsicurl/...` and `/vsizip/vsicurl/...` both fail to
# open, on or off the lockdown below), but `ST_Read` handles a plain
# `https://` URL itself via DuckDB's own httpfs filesystem — the same code
# path `read_parquet`/`read_csv` use, and the one the warmup below exercises.
_NATURAL_EARTH_COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_admin_0_countries.geojson"
)
_NATURAL_EARTH_PLACES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_110m_populated_places.geojson"
)

#: Conservative resource caps for a shared, always-on connection. Sized
#: against this toolset's `toolset.yaml` pod memory limit, with headroom for
#: Python/uvicorn overhead alongside DuckDB itself.
_MEMORY_LIMIT = "700MB"
_THREADS = 2

# Overture Maps places, read REMOTELY from the public bucket. This
# deliberately walks back part of the "drop Overture" decision (see the module
# docstring's SCOPE section): the measured limits there still hold, but the
# geo workflow tools in `geo_tools.py` only ever touch it with a bbox
# predicate, which prunes on parquet row-group stats. Pruning needs every
# file's footer, and reading those cold measured ~73s against the
# 2026-07-22.0 release — so `parquet_metadata_cache` is enabled and
# `_warm_overture_footers` pays that cost once, on a background thread
# started at import. Footer-warm timings: `LIMIT 3` ~2s, a neighborhood-scale bbox
# (~0.03°x0.02°) name search ~18s, a city-scale bbox (~0.2°x0.12°) name
# search ~160s. That is why `geo_tools` runs under LOOKUP_TIMEOUT_SECONDS
# rather than the generic 30s watchdog, and why `list_sources` tells callers
# to always filter on the `bbox` column. An unfiltered aggregate still times
# out, which is the accepted MVP trade-off — a local extract (just another
# CREATE VIEW here) remains the fix when this graduates from MVP.
_OVERTURE_RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-07-22.0")
_OVERTURE_PLACES_PATH = (
    "s3://overturemaps-us-west-2/release/"
    f"{_OVERTURE_RELEASE}/theme=places/type=place/*.parquet"
)


class ColumnInfo(TypedDict):
    """One column of a curated source, as advertised by ``list_sources``."""

    name: str
    type: str
    description: NotRequired[str]
    #: Vega-Lite encoding channels this column tends to work well as.
    good_for: NotRequired[list[str]]


class SourceInfo(TypedDict):
    """One pre-registered source, as advertised by ``list_sources``."""

    name: str
    kind: str  # "view" (curated, fixed schema) | "table_function" (SQL fn)
    description: str
    example_sql: str
    columns: NotRequired[list[ColumnInfo]]


def _build_connection() -> duckdb.DuckDBPyConnection:
    started = time.perf_counter()
    logger.info(
        "building DuckDB connection (memory_limit=%s, threads=%d, overture_release=%s)",
        _MEMORY_LIMIT,
        _THREADS,
        _OVERTURE_RELEASE,
    )
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit = '{_MEMORY_LIMIT}'")
    con.execute(f"SET threads = {_THREADS}")

    # Core extensions only, and community extensions refused before the first
    # INSTALL — see the module docstring, layer "Extensions".
    logger.debug("installing and loading extensions: httpfs, spatial")
    con.execute("SET allow_community_extensions = false")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute("INSTALL spatial")
    con.execute("LOAD spatial")

    # Default region for ad hoc `s3://` reads. Public/anonymous buckets need
    # no credentials, just a region. NOTE: `lock_configuration` below means a
    # caller cannot change this, so an `s3://` URL in a different region will
    # fail — use that bucket's `https://` endpoint instead, or add a region
    # here. Plain `https://` reads are unaffected.
    con.execute("SET s3_region = 'us-east-1'")

    # Off by default in DuckDB; without it every query against the Overture
    # glob re-reads every file's parquet footer (~73s, measured — see the
    # Overture notes above). With it, the startup warmup below makes bbox
    # pruning effectively free for the life of this connection.
    con.execute("SET parquet_metadata_cache = true")

    # Reusing a kept-alive connection S3 has meanwhile closed wedged a
    # footer scan indefinitely (observed: sockets in CLOSE-WAIT, one thread
    # polling forever, ~14 min and counting before it was killed). A fresh
    # connection per request costs TLS handshakes but cannot be handed a
    # dead socket.
    con.execute("SET http_keep_alive = false")

    # The Overture bucket lives in us-west-2; a SCOPEd, credential-less
    # secret pins the region for that bucket alone, so the default above
    # keeps serving everything else. Created before `lock_configuration`,
    # and caller SQL can never run CREATE (see security.py), so it is
    # immutable at runtime like every other setting here.
    con.execute(
        "CREATE SECRET overture (TYPE s3, REGION 'us-west-2', "
        "SCOPE 's3://overturemaps-us-west-2')"
    )

    _warm_up_remote_filesystem(con)
    _register_views(con)

    # Lock down. See the module docstring, layer "DuckDB is configured...",
    # for why this order (and the warmup above) is load-bearing.
    con.execute("SET disabled_filesystems = 'LocalFileSystem'")
    con.execute("SET lock_configuration = true")
    logger.info(
        "DuckDB connection ready and locked down in %.1fs",
        time.perf_counter() - started,
    )
    return con


def _warm_up_remote_filesystem(con: duckdb.DuckDBPyConnection) -> None:
    """Touch DuckDB's remote filesystem path once before locking local disk.

    See the module docstring for why this is necessary and what it's based
    on. Any tiny, reliably-reachable remote read does the job. This reads a
    URL the connection already depends on (a Natural Earth view source)
    rather than an unrelated one: an extra third-party host is one more way
    for startup to fail, and the explicit call here still keeps the
    warmup-before-lockdown ordering self-documenting instead of incidental
    to view registration order.
    """
    started = time.perf_counter()
    logger.debug("warming up remote filesystem via %s", _NATURAL_EARTH_PLACES_URL)
    # S608: the only interpolated value is a module-constant URL, never
    # caller input — same false positive as the view SQL below.
    con.execute(f"SELECT 1 FROM ST_Read('{_NATURAL_EARTH_PLACES_URL}') LIMIT 1")  # noqa: S608
    logger.debug(
        "remote filesystem warmup done in %.1fs", time.perf_counter() - started
    )


def _register_views(con: duckdb.DuckDBPyConnection) -> None:
    # Each view's SQL is built as a plain string first and executed on its
    # own line, rather than inlining `con.execute(f"""..."""), so
    # the linter-suppression comment below has an unambiguous line to
    # attach to instead of landing inside the SQL string itself. The
    # underlying ruff finding ("possible SQL injection") is a false
    # positive: the only interpolated values here are module constants
    # (hardcoded dataset URLs) — never caller input, which is what actually
    # makes string-built SQL dangerous.
    countries_sql = f"""
        CREATE VIEW natural_earth_countries AS
        SELECT
            "NAME" AS name,
            "ADMIN" AS admin_name,
            "ISO_A2" AS iso_a2,
            "ISO_A3" AS iso_a3,
            "CONTINENT" AS continent,
            "SUBREGION" AS subregion,
            "POP_EST" AS population_estimate,
            "GDP_MD" AS gdp_million_usd,
            "INCOME_GRP" AS income_group,
            geom AS geometry
        FROM ST_Read('{_NATURAL_EARTH_COUNTRIES_URL}')
    """  # noqa: S608
    logger.debug("registering view natural_earth_countries")
    con.execute(countries_sql)

    places_ne_sql = f"""
        CREATE VIEW natural_earth_places AS
        SELECT
            "NAME" AS name,
            "ADM0NAME" AS country_name,
            "ISO_A2" AS iso_a2,
            "LATITUDE" AS latitude,
            "LONGITUDE" AS longitude,
            "POP_MAX" AS population_max,
            "TIMEZONE" AS timezone,
            geom AS geometry
        FROM ST_Read('{_NATURAL_EARTH_PLACES_URL}')
    """  # noqa: S608
    logger.debug("registering view natural_earth_places")
    con.execute(places_ne_sql)

    overture_sql = f"""
        CREATE VIEW overture_places AS
        SELECT *
        FROM read_parquet('{_OVERTURE_PLACES_PATH}', hive_partitioning=1)
    """  # noqa: S608
    logger.debug("registering view overture_places (%s)", _OVERTURE_PLACES_PATH)
    con.execute(overture_sql)


def _warm_overture_footers(con: duckdb.DuckDBPyConnection) -> None:
    """Pull every Overture parquet footer into the metadata cache, once.

    A stats-only count against an empty bbox touches each file's row-group
    statistics and reads no data columns. ~73s measured cold — which is why
    this runs on a background thread (below) rather than blocking import:
    the Helm chart's liveness probe has no initial delay, so a server that
    takes 73s to bind `/health` would be killed and crash-loop. On a cursor,
    so it can run concurrently with early tool calls; a call that arrives
    before the warmup finishes just pays (part of) this cost itself, inside
    geo_tools' LOOKUP_TIMEOUT_SECONDS budget.

    Bounded, exactly like a user query: an S3 stall wedged an unbounded
    version of this scan indefinitely (the CLOSE-WAIT incident described at
    ``http_keep_alive`` above), and a wedged warmup would hang anything that
    joins the thread. The timer interrupts the cursor the same way
    ``execute_capped``'s watchdog does. Best-effort by design: on timeout or
    failure the first real query re-pays whatever footer cost remains —
    partial progress stays cached — so nothing is broken, only slow.
    Requires ``parquet_metadata_cache = true`` to be worth anything.
    """
    started = time.perf_counter()
    logger.info(
        "warming Overture parquet footer cache in the background "
        "(budget %.0fs; queries arriving before it finishes run slower)",
        LOOKUP_TIMEOUT_SECONDS,
    )
    cursor = con.cursor()
    timer = threading.Timer(LOOKUP_TIMEOUT_SECONDS, cursor.interrupt)
    timer.start()
    try:
        cursor.execute(
            "SELECT count(*) FROM overture_places "
            "WHERE bbox.xmin > 0 AND bbox.xmax < 0.001 "
            "AND bbox.ymin > 0 AND bbox.ymax < 0.001"
        )
    except duckdb.Error as error:
        # Best-effort by design (see docstring): partial progress stays
        # cached and the first real query re-pays the rest — but say so,
        # or a slow first query has no explanation in the logs.
        logger.warning(
            "Overture footer warmup stopped after %.1fs (%s: %s); "
            "the first queries will be slower",
            time.perf_counter() - started,
            type(error).__name__,
            error,
        )
    else:
        logger.info(
            "Overture footer warmup finished in %.1fs",
            time.perf_counter() - started,
        )
    finally:
        timer.cancel()


# Hand-authored notes on top of each view's real, live-introspected schema
# (see _build_sources): the things a schema dump can't tell an LLM, like
# which columns make good chart channels. A column with no entry here still
# appears in list_sources with just its name/type.
_COLUMN_NOTES: dict[str, dict[str, ColumnInfo]] = {
    "natural_earth_countries": {
        "name": {
            "name": "name",
            "type": "",
            "description": "Country name.",
            "good_for": ["x", "color"],
        },
        "continent": {
            "name": "continent",
            "type": "",
            "good_for": ["x", "color"],
        },
        "population_estimate": {
            "name": "population_estimate",
            "type": "",
            "good_for": ["y", "color"],
        },
        "gdp_million_usd": {
            "name": "gdp_million_usd",
            "type": "",
            "description": "GDP estimate, millions of USD.",
            "good_for": ["y", "color"],
        },
        "income_group": {
            "name": "income_group",
            "type": "",
            "good_for": ["x", "color"],
        },
    },
    "natural_earth_places": {
        "name": {
            "name": "name",
            "type": "",
            "description": "City/place name.",
            "good_for": ["color"],
        },
        "latitude": {"name": "latitude", "type": "", "good_for": ["y"]},
        "longitude": {"name": "longitude", "type": "", "good_for": ["x"]},
        "population_max": {
            "name": "population_max",
            "type": "",
            "description": "High estimate of the place's population.",
            "good_for": ["y", "color"],
        },
    },
    "overture_places": {
        "names": {
            "name": "names",
            "type": "",
            "description": "Struct of names; `names.primary` is the display name.",
        },
        "categories": {
            "name": "categories",
            "type": "",
            "description": (
                "Struct of Overture category slugs; filter on "
                "`categories.primary` (e.g. 'restaurant', 'cafe', 'bar')."
            ),
        },
        "bbox": {
            "name": "bbox",
            "type": "",
            "description": (
                "Struct of xmin/xmax/ymin/ymax. ALWAYS filter on these — "
                "they prune the remote scan; without them queries run for "
                "minutes and time out."
            ),
        },
        "confidence": {
            "name": "confidence",
            "type": "",
            "description": "Overture's 0-1 existence confidence.",
            "good_for": ["y", "color"],
        },
        "geometry": {
            "name": "geometry",
            "type": "",
            "description": "Point geometry; wrap in ST_AsGeoJSON to read it.",
        },
    },
}

_VIEW_DESCRIPTIONS = {
    "natural_earth_countries": (
        "Natural Earth 1:110m country polygons with basic demographic/"
        "economic attributes (name, continent, population, GDP, income "
        "group). Small and fast — a full GROUP BY over it returns in well "
        "under a second. Good for country- and continent-level breakdowns."
    ),
    "natural_earth_places": (
        "Natural Earth 1:110m populated places (major world cities) as "
        "points with lat/lon and population — a small, fast dataset good "
        "for quick geographic scatter charts."
    ),
    "overture_places": (
        f"Overture Maps places (POIs), release {_OVERTURE_RELEASE}, read "
        "remotely from the public bucket. LARGE and SLOW: every query MUST "
        "filter on the bbox struct (bbox.xmin/xmax/ymin/ymax) to prune the "
        "scan, and even a city-scale bbox takes minutes. Prefer the "
        "dedicated get_place/places_within_area tools; use SQL against this "
        "view only for what they cannot express, over a small bbox."
    ),
}

_EXAMPLE_SQL = {
    "natural_earth_countries": (
        "SELECT name, continent, population_estimate FROM "
        "natural_earth_countries ORDER BY population_estimate DESC LIMIT 20"
    ),
    "natural_earth_places": (
        "SELECT name, longitude, latitude, population_max FROM "
        "natural_earth_places ORDER BY population_max DESC LIMIT 50"
    ),
    "overture_places": (
        "SELECT names.primary AS name, categories.primary AS category, "
        "ST_AsGeoJSON(geometry) AS geometry FROM overture_places "
        "WHERE bbox.xmin > -9.16 AND bbox.xmax < -9.13 "
        "AND bbox.ymin > 38.70 AND bbox.ymax < 38.72 LIMIT 10"
    ),
}


def _describe_view(con: duckdb.DuckDBPyConnection, view: str) -> list[ColumnInfo]:
    """Live column name/type for a view, merged with any curated notes.

    Reads the real, already-registered view's schema rather than
    hand-listing columns, so this catalog can't drift from what the views
    actually return.
    """
    notes = _COLUMN_NOTES.get(view, {})
    columns: list[ColumnInfo] = []
    for name, duck_type, *_ in con.execute(f"DESCRIBE {view}").fetchall():
        note = notes.get(name, ColumnInfo(name=name, type=""))
        columns.append(ColumnInfo(**{**note, "name": name, "type": str(duck_type)}))
    return columns


def _build_sources(con: duckdb.DuckDBPyConnection) -> list[SourceInfo]:
    return [
        SourceInfo(
            name=view,
            kind="view",
            description=_VIEW_DESCRIPTIONS[view],
            example_sql=_EXAMPLE_SQL[view],
            columns=_describe_view(con, view),
        )
        for view in (
            "natural_earth_countries",
            "natural_earth_places",
            "overture_places",
        )
    ]


async def execute_capped(
    sql: str, params: list[Any] | None = None, timeout: float | None = None
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute SQL on a fresh cursor, bounded by a wall-clock watchdog.

    The one way any tool runs SQL against :data:`CON`. Runs on a
    ``CON.cursor()`` (cheap; shares the locked-down in-memory database, safe
    concurrently) inside ``asyncio.to_thread``, guarded by a watchdog that
    interrupts the cursor past ``timeout`` seconds — DuckDB has no native
    query timeout. Raises ``duckdb.Error`` (``InterruptException`` on
    timeout) for the caller to map to a ``ToolError``.

    ``timeout=None`` means ``security.QUERY_TIMEOUT_SECONDS``; ``geo_tools``
    passes its longer ``LOOKUP_TIMEOUT_SECONDS`` budget (see the Overture
    timing notes above ``_OVERTURE_RELEASE``).
    """
    budget = QUERY_TIMEOUT_SECONDS if timeout is None else timeout
    cursor = CON.cursor()
    finished = asyncio.Event()
    started = time.perf_counter()
    logger.debug(
        "executing (budget %.0fs): %s  params=%s", budget, _oneline(sql), params
    )

    async def watchdog() -> None:
        try:
            await asyncio.wait_for(finished.wait(), timeout=budget)
        except TimeoutError:
            logger.warning(
                "query exceeded its %.0fs budget, interrupting: %s",
                budget,
                _oneline(sql),
            )
            cursor.interrupt()

    watchdog_task = asyncio.create_task(watchdog())
    try:
        result = await asyncio.to_thread(cursor.execute, sql, params)
        rows = await asyncio.to_thread(result.fetchall)
        columns = [description[0] for description in result.description]
    except duckdb.Error as error:
        logger.debug(
            "query failed after %.1fs (%s: %s)",
            time.perf_counter() - started,
            type(error).__name__,
            error,
        )
        raise
    finally:
        finished.set()
        await watchdog_task
    logger.debug(
        "query returned %d row(s), %d column(s) in %.1fs",
        len(rows),
        len(columns),
        time.perf_counter() - started,
    )
    return columns, rows


CON = _build_connection()
SOURCES = _build_sources(CON)

#: Kicked off at import, joined by nothing: tool calls that beat it simply
#: run slower (see ``_warm_overture_footers``). Tests join it to measure
#: warm-path behavior deterministically.
FOOTER_WARMUP = threading.Thread(
    target=_warm_overture_footers, args=(CON,), daemon=True
)
FOOTER_WARMUP.start()
