"""Tests for duckdb-analyst.

These hit the real network — DuckDB's own httpfs/spatial extensions have no
mockable transport the way `httpx.MockTransport` covers `stac-explorer`.
Fixtures are picked to be small and stable: a single ad hoc parquet file
DuckDB's own docs use as a demo (~500 bytes), and the curated Natural Earth
1:110m views (a few hundred KB each, so a full aggregate over one is still
sub-second).

Every curated source this toolset advertises is exercised here with a real
query. That is deliberate: an earlier revision advertised Overture Maps and
a STAC table function in `list_sources` without covering either with a
query test, and both turned out to be unusable in practice (Overture
aggregates blew the 30s timeout; STAC search returned HTTP 422). If you add
a source to `connection.py`, add a query test for it here, or it does not
ship.

The security regression suite is the important part: every case there must
come back as a `ToolError`, never rows.
"""

from mcp_runtime.tool_result import is_error

from duckdb_analyst.tools import chart, list_sources, query

# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_list_sources_covers_every_dataset_family():
    result = list_sources.invoke({})
    names = {source["name"] for source in result["sources"]}
    assert {
        "natural_earth_countries",
        "natural_earth_places",
        "overture_places",
    } <= names
    for source in result["sources"]:
        assert source["description"]
        assert source["example_sql"]


def test_list_sources_advertises_nothing_untested():
    """Guard the docstring's rule: no advertised source without a query test.

    `list_sources` is what an LLM reads to decide what it can query, so an
    entry here that no test exercises is how the Overture/STAC regression
    got shipped in the first place. Overture is back (remote, bbox-pruned,
    metadata-cache-warmed — see connection.py's Overture notes) and is
    query-tested below on the same neighborhood-scale-bbox terms the
    curated geo tools use.
    """
    tested = {"natural_earth_countries", "natural_earth_places", "overture_places"}
    advertised = {source["name"] for source in list_sources.invoke({})["sources"]}
    assert advertised == tested, (
        f"sources advertised but not query-tested: {advertised - tested}"
    )


def test_list_sources_flags_chart_friendly_columns():
    result = list_sources.invoke({})
    countries = next(
        s for s in result["sources"] if s["name"] == "natural_earth_countries"
    )
    continent = next(c for c in countries["columns"] if c["name"] == "continent")
    assert "x" in continent["good_for"] or "color" in continent["good_for"]


async def test_query_against_curated_view():
    result = await query.ainvoke(
        {
            "sql": (
                "SELECT name, continent, population_estimate "
                "FROM natural_earth_countries ORDER BY population_estimate DESC"
            ),
            "limit": 5,
        }
    )
    assert not is_error(result)
    assert 0 < len(result["rows"]) <= 5
    assert result["row_count"] == len(result["rows"])
    assert "name" in result["rows"][0]


async def test_query_supports_full_aggregate_over_curated_view():
    """A whole-view GROUP BY must finish well inside the query timeout.

    This is the property that makes the curated sources worth curating —
    the Overture views this replaced could not do it.
    """
    result = await query.ainvoke(
        {
            "sql": (
                "SELECT continent, COUNT(*) AS n_countries, "
                "SUM(population_estimate) AS population "
                "FROM natural_earth_countries GROUP BY continent "
                "ORDER BY population DESC"
            )
        }
    )
    assert not is_error(result)
    assert len(result["rows"]) > 1
    assert result["rows"][0]["population"] > 0


async def test_query_against_second_curated_view():
    result = await query.ainvoke(
        {
            "sql": (
                "SELECT name, country_name, population_max, longitude, latitude "
                "FROM natural_earth_places ORDER BY population_max DESC"
            ),
            "limit": 3,
        }
    )
    assert not is_error(result)
    assert len(result["rows"]) == 3
    assert result["rows"][0]["population_max"] >= result["rows"][-1]["population_max"]


async def test_query_can_use_spatial_functions_on_geometry():
    """`spatial` is loaded, so ST_* works on the views' geometry column."""
    result = await query.ainvoke(
        {
            "sql": (
                "SELECT name, ROUND(ST_Area(geometry), 2) AS area_deg2 "
                "FROM natural_earth_countries WHERE name = 'Brazil'"
            )
        }
    )
    assert not is_error(result)
    assert result["rows"][0]["area_deg2"] > 0


async def test_query_against_overture_places_view():
    """The Overture view answers a bbox-pruned query, warm or warming.

    ALWAYS bbox-filtered, per the view's own description — an unfiltered
    query against the remote theme is the documented, accepted way to time
    out. Joins the footer warmup first (bounded — it interrupts itself), and
    then retries the 30s-budget query a few times: the parquet metadata
    cache keeps whatever footers each attempt managed to read, so on a slow
    network successive timeouts converge on a warm cache rather than
    starting over. One attempt sufficed warm (~2s measured); a cold, slow
    run has been observed to need the warmup bound plus one more attempt.
    """
    from duckdb_analyst.connection import FOOTER_WARMUP
    from duckdb_analyst.security import LOOKUP_TIMEOUT_SECONDS

    FOOTER_WARMUP.join(timeout=LOOKUP_TIMEOUT_SECONDS + 30)
    for _ in range(6):
        result = await query.ainvoke(
            {
                "sql": (
                    "SELECT names.primary AS name FROM overture_places "
                    "WHERE bbox.xmin > -9.16 AND bbox.xmax < -9.13 "
                    "AND bbox.ymin > 38.70 AND bbox.ymax < 38.72"
                ),
                "limit": 3,
            }
        )
        if not (is_error(result) and result["error"] == "timeout"):
            break
    assert not is_error(result)
    assert len(result["rows"]) == 3
    assert result["rows"][0]["name"]


async def test_query_against_ad_hoc_public_parquet_url():
    result = await query.ainvoke(
        {
            "sql": "SELECT * FROM read_parquet('https://duckdb.org/data/holdings.parquet')"
        }
    )
    assert not is_error(result)
    assert len(result["rows"]) > 0


async def test_query_enforces_hard_row_cap_server_side():
    result = await query.ainvoke(
        {
            "sql": "SELECT * FROM range(100000) AS t(n)",
            "limit": 1_000_000,  # above MAX_ROW_LIMIT
        }
    )
    assert not is_error(result)
    assert result["row_count"] <= 10_000


async def test_chart_fills_in_data_values_and_preserves_spec():
    spec = {
        "mark": "bar",
        "encoding": {
            "x": {"field": "continent", "type": "nominal", "sort": "-y"},
            "y": {"field": "population", "type": "quantitative"},
        },
    }
    result = await chart.ainvoke(
        {
            "sql": (
                "SELECT continent, SUM(population_estimate) AS population "
                "FROM natural_earth_countries GROUP BY continent "
                "ORDER BY population DESC"
            ),
            "spec": spec,
            "limit": 10,
        }
    )
    assert not is_error(result)
    assert result["spec"]["mark"] == "bar"
    assert result["spec"]["encoding"] == spec["encoding"]
    values = result["spec"]["data"]["values"]
    assert 0 < len(values) <= 10
    # The caller's encoding must line up with the columns the SQL returned,
    # or the spec renders empty in the client.
    assert {"continent", "population"} <= set(values[0])


# ---------------------------------------------------------------------------
# Security regression suite: every one of these must be rejected, never
# silently succeed.
# ---------------------------------------------------------------------------


async def test_rejects_local_file_read_via_read_text_etc_passwd():
    result = await query.ainvoke({"sql": "SELECT * FROM read_text('/etc/passwd')"})
    assert is_error(result)


async def test_rejects_local_file_read_via_read_text_proc_environ():
    result = await query.ainvoke(
        {"sql": "SELECT * FROM read_text('/proc/self/environ')"}
    )
    assert is_error(result)


async def test_rejects_multiple_statements():
    result = await query.ainvoke({"sql": "SELECT 1; ATTACH ':memory:' AS x"})
    assert is_error(result)


async def test_rejects_install():
    result = await query.ainvoke({"sql": "INSTALL icu"})
    assert is_error(result)


async def test_rejects_attempt_to_loosen_locked_configuration():
    result = await query.ainvoke({"sql": "SET enable_external_access = true"})
    assert is_error(result)


async def test_rejects_pragma():
    result = await query.ainvoke({"sql": "PRAGMA database_list"})
    assert is_error(result)


async def test_rejects_duckdb_secrets():
    result = await query.ainvoke({"sql": "SELECT * FROM duckdb_secrets()"})
    assert is_error(result)
