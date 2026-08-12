"""LangChain tools for duckdb-analyst: sandbox-free SQL analysis over public
parquet datasets via a security-hardened, read-only DuckDB connection.

Three tools, deliberately minimal: explore what's available (`list_sources`),
run a SELECT (`query`), or run one and drop the rows straight into a
caller-supplied Vega-Lite spec (`chart`). No bespoke per-dataset tool — a new
dataset is a `CREATE VIEW` in `connection.py`, not a fourth tool, and it
becomes reachable through `query`/`chart` and discoverable via `list_sources`
(see `connection.SOURCES`).

The actual security model (why arbitrary SQL is safe to run here) lives in
`connection.py`'s module docstring — read that before changing anything here.
This module's own job is narrower: reject obviously-wrong SQL early
(`security.validate_select_only`), enforce the row cap server-side, bound
execution time, and turn DuckDB's own errors into `ToolError`s instead of
raising.
"""

import json
from typing import Any, NotRequired

import duckdb
from langchain_core.tools import tool

from mcp_runtime.tool_result import ToolError, ToolResult

from duckdb_analyst.connection import SOURCES, SourceInfo, execute_capped
from duckdb_analyst.geo_tools import GEO_TOOLS
from duckdb_analyst.security import clamp_limit, validate_select_only


class ListSourcesResult(ToolResult):
    """The pre-registered views/table functions available to query/chart."""

    sources: NotRequired[list[SourceInfo]]


class QueryResult(ToolResult):
    """Rows from a validated, capped SELECT against the DuckDB connection."""

    rows: NotRequired[list[dict[str, Any]]]
    row_count: NotRequired[int]


class ChartResult(ToolResult):
    """A caller-supplied Vega-Lite mark/encoding spec with data filled in."""

    spec: NotRequired[dict[str, Any]]


def _json_safe(value: Any) -> Any:
    """Round-trip a DuckDB row through JSON to flatten it into plain types.

    DuckDB hands back Decimals, dates/times, raw WKB bytes for geometry
    columns, and nested struct/list values as Python dicts/lists — none of
    that is guaranteed JSON-native. Rather than hand-writing a type-by-type
    converter (and inevitably missing one), `default=str` is the catch-all:
    anything `json.dumps` doesn't already know becomes its `str()`. That
    makes e.g. a raw geometry blob a somewhat ugly `"b'\\x01\\x03...'"`
    string rather than a serialization crash — `list_sources` tells callers
    to wrap geometry columns in `ST_AsText`/`ST_AsGeoJSON` instead of relying
    on this fallback for anything they actually want to read.
    """
    return json.loads(json.dumps(value, default=str))


def _wrap_with_limit(sql: str, limit: int) -> str:
    """Force a hard row cap at the SQL level, not just a Python-side slice.

    Wrapping in a subquery caps rows *inside* DuckDB regardless of whether
    the caller's own SQL has a LIMIT — a caller can't get more rows back by
    simply omitting one. It also means the top-level statement DuckDB
    actually executes is always a SELECT ... FROM (...), which is one more
    reason (beyond `validate_select_only`) that stray non-SELECT SQL can't
    reach execution here.
    """
    # Not a real injection vector, despite how this looks to a linter: `sql`
    # has already passed `validate_select_only` (single SELECT/WITH
    # statement) by the time this runs, and `limit` is an int already
    # clamped by `clamp_limit` — never a caller-controlled string spliced in
    # verbatim.
    return f"SELECT * FROM (\n{sql}\n) AS _duckdb_analyst_query\nLIMIT {limit}"  # noqa: S608


async def _run_query(
    sql: str, limit: int
) -> tuple[list[str], list[dict[str, Any]]] | ToolError:
    """Validate, execute and fetch a capped SELECT, or return a ToolError.

    Execution (fresh cursor, watchdog timeout) is `connection.execute_capped`;
    this wrapper owns what is specific to caller-supplied SQL — the
    statement-shape validation, the row cap, and the ToolError mapping.
    """
    if detail := validate_select_only(sql):
        return ToolError(error="invalid_query", detail=detail)

    capped_limit = clamp_limit(limit)
    executable = _wrap_with_limit(sql, capped_limit)
    try:
        columns, rows = await execute_capped(executable)
    except duckdb.Error as error:
        kind = (
            "timeout"
            if isinstance(error, duckdb.InterruptException)
            else "query_failed"
        )
        return ToolError(error=kind, detail=str(error))

    records = [_json_safe(dict(zip(columns, row, strict=True))) for row in rows]
    return columns, records


@tool
def list_sources() -> ListSourcesResult:
    """List the pre-registered views available to `query`/`chart`: Natural
    Earth countries and populated places, plus Overture Maps places — with
    column descriptions and which columns work well as x/y/color chart
    channels. Call this before writing SQL against an unfamiliar source.

    `query`/`chart` are not limited to these: they also read any public
    `https://` or `s3://` parquet or CSV URL via `read_parquet`/`read_csv`.
    """
    names = ", ".join(source["name"] for source in SOURCES)
    return ListSourcesResult(
        message=f"{len(SOURCES)} source(s) available: {names}.", sources=SOURCES
    )


@tool
async def query(sql: str, limit: int = 1000) -> QueryResult | ToolError:
    """Run a read-only SQL SELECT against the curated views from
    `list_sources`, or an ad hoc `https://`/`s3://` parquet/CSV URL via
    `read_parquet`/`read_csv`, and return the rows as JSON records.

    Only a single `SELECT`/`WITH` statement is allowed. `limit` caps the
    rows returned (default 1000, hard max 10000), enforced regardless of
    what the query itself requests.
    """
    outcome = await _run_query(sql, limit)
    if isinstance(outcome, dict):  # ToolError
        return outcome
    _columns, rows = outcome
    return QueryResult(
        message=f"{len(rows)} row(s) returned.", rows=rows, row_count=len(rows)
    )


@tool
async def chart(
    sql: str, spec: dict[str, Any], limit: int = 1000
) -> ChartResult | ToolError:
    """Run a SQL query the same way `query` does, then return a completed
    Vega-Lite spec: `spec` (your `mark`/`encoding`, no `data` key) with the
    query's rows filled in as `spec.data.values`. Rendering is left to
    whatever client receives the result — this tool only assembles the spec.

    `limit` caps the rows inlined into the chart (default 1000, hard max
    10000) — keep it small; a Vega-Lite spec with tens of thousands of
    inlined rows is unwieldy for most renderers.
    """
    outcome = await _run_query(sql, limit)
    if isinstance(outcome, dict):  # ToolError
        return outcome
    _columns, rows = outcome
    full_spec = {**spec, "data": {"values": rows}}
    return ChartResult(
        message=f"Chart spec built with {len(rows)} row(s) of data.", spec=full_spec
    )


TOOLS = [list_sources, query, chart, *GEO_TOOLS]
