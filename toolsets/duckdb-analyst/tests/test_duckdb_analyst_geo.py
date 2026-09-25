"""Tests for the geo workflow tools (`geo_tools.py`).

The network tests here hit the real remote Overture bucket, like the rest of
this toolset's suite hits real Natural Earth URLs — there is no mockable
transport under DuckDB's httpfs. They stay fast by joining the footer-warmup
thread once per session (the same state a warmed-up pod is in) and using
neighborhood-scale bboxes: warm, `get_place` measured ~6s and
`places_within_area` ~1s. The pure-computation buffer tool is tested offline.

The state-wiring test is the one to keep green above all: it pins the
provenance contract — geometries move between these tools through session
state, never through the model — which is the reason this workflow exists.
"""

import pytest

from mcp_runtime.declarations import not_authored, output_fields
from mcp_runtime.tool_result import is_error

from duckdb_analyst.geo_tools import (
    get_place,
    get_search_area,
    places_within_area,
)

# Neighborhood-scale test fixture: the blocks around Lisbon's Time Out
# Market — small enough that a bbox-pruned remote scan is seconds, and a
# stable, famous venue to match against.
LISBON_BBOX = [-9.16, 38.70, -9.13, 38.72]


@pytest.fixture(scope="session", autouse=True)
def warm_footers():
    """Join the import-time footer warmup, so timings here are warm-path.

    Bounded join: the warmup interrupts itself after LOOKUP_TIMEOUT_SECONDS,
    so waiting a hair longer than that can never hang the suite — if the
    bound is hit, the tests run cold-path and slower, which is the same
    degradation a real pod would serve.
    """
    from duckdb_analyst.connection import FOOTER_WARMUP
    from duckdb_analyst.security import LOOKUP_TIMEOUT_SECONDS

    FOOTER_WARMUP.join(timeout=LOOKUP_TIMEOUT_SECONDS + 30)


# ---------------------------------------------------------------------------
# The provenance contract
# ---------------------------------------------------------------------------


def test_state_wiring_forms_the_place_to_area_chain():
    """get_place → get_search_area → places_within_area, linked by state.

    These assertions are what makes the geometries flow through session
    state instead of the model's context. Dropping a `NotAuthored` tag or a
    result field silently degrades the chain to model-passed values, so any
    change here must be deliberate.
    """
    assert "place" in output_fields(get_place)
    assert not_authored(get_search_area) == ["place"]
    assert "search_area" in output_fields(get_search_area)
    assert not_authored(places_within_area) == ["area"]


# ---------------------------------------------------------------------------
# get_search_area — pure computation, offline
# ---------------------------------------------------------------------------


def _place(lon: float = -9.145, lat: float = 38.707) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"name": "Time Out Market Lisboa", "overture_id": "x"},
    }


def test_get_search_area_buffers_true_kilometers():
    result = get_search_area.invoke({"buffer_km": 1.0, "place": _place()})
    assert not is_error(result)
    area = result["search_area"]
    assert area["type"] == "FeatureCollection"
    ring = area["features"][0]["geometry"]["coordinates"][0]
    lons = [point[0] for point in ring]
    lats = [point[1] for point in ring]
    # 2 km across: ~0.018° of latitude anywhere; longitude widens by
    # 1/cos(lat). Loose 10% tolerance — this guards the projection choice
    # (a Web Mercator buffer would be ~28% small at Lisbon), not shapely.
    assert max(lats) - min(lats) == pytest.approx(2 / 111.2, rel=0.1)
    assert max(lons) - min(lons) == pytest.approx(2 / 87.0, rel=0.1)


def test_get_search_area_carries_place_properties():
    result = get_search_area.invoke({"buffer_km": 2.0, "place": _place()})
    properties = result["search_area"]["features"][0]["properties"]
    assert properties["name"] == "Time Out Market Lisboa"
    assert properties["buffer_km"] == 2.0


def test_get_search_area_rejects_oversized_buffer():
    result = get_search_area.invoke({"buffer_km": 26.0, "place": _place()})
    assert is_error(result)
    assert result["error"] == "invalid_buffer"


def test_get_search_area_rejects_place_without_geometry():
    result = get_search_area.invoke({"buffer_km": 1.0, "place": {"type": "Feature"}})
    assert is_error(result)
    assert result["error"] == "invalid_place"


# ---------------------------------------------------------------------------
# get_place — remote Overture, warm path
# ---------------------------------------------------------------------------


async def test_get_place_finds_a_famous_venue():
    result = await get_place.ainvoke(
        {"place_name": "Time Out Market", "search_bbox": LISBON_BBOX}
    )
    assert not is_error(result)
    place = result["place"]
    assert place["type"] == "Feature"
    assert "Time Out" in place["properties"]["name"]
    assert place["geometry"]["type"] == "Point"
    lon, lat = place["geometry"]["coordinates"]
    assert LISBON_BBOX[0] < lon < LISBON_BBOX[2]
    assert LISBON_BBOX[1] < lat < LISBON_BBOX[3]


async def test_get_place_rejects_malformed_bbox():
    result = await get_place.ainvoke(
        {"place_name": "anything", "search_bbox": [1.0, 2.0, 3.0]}
    )
    assert is_error(result)
    assert result["error"] == "invalid_bbox"


async def test_get_place_reports_no_match_as_not_found():
    result = await get_place.ainvoke(
        {
            "place_name": "Zzyzzx Qwertyuiop Pavilion",
            "search_bbox": LISBON_BBOX,
        }
    )
    assert is_error(result)
    assert result["error"] == "not_found"


# ---------------------------------------------------------------------------
# places_within_area — remote Overture, warm path
# ---------------------------------------------------------------------------


async def test_places_within_area_full_chain():
    """The workflow end to end, exactly as state would drive it."""
    found = await get_place.ainvoke(
        {"place_name": "Time Out Market", "search_bbox": LISBON_BBOX}
    )
    assert not is_error(found)
    area = get_search_area.invoke({"buffer_km": 0.5, "place": found["place"]})
    assert not is_error(area)
    result = await places_within_area.ainvoke(
        {"category": "cafes", "area": area["search_area"], "limit": 5}
    )
    assert not is_error(result)
    features = result["places"]["features"]
    assert 0 < len(features) <= 5
    assert all(f["properties"]["category"] == "cafe" for f in features)
    assert "cafe" in result["message"]
    # A busy half-kilometer of Lisbon has more than five cafes, so the
    # message must not present five as the total.
    assert "not the total" in result["message"]


async def test_places_within_area_rejects_non_geojson_area():
    result = await places_within_area.ainvoke(
        {"category": "cafe", "area": {"bogus": True}}
    )
    assert is_error(result)
    assert result["error"] == "invalid_area"
