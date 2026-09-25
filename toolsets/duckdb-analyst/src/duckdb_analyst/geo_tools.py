"""The geo-assistant workflow, as tools whose state flows through session state.

Three tools ported from geo-assistant's LangGraph agent, whose state flow
(place → search area → places within it) maps directly onto session state:
``get_place`` publishes the matched place feature (its ``place`` result key),
``get_search_area`` takes it as a ``NotAuthored`` parameter and publishes an
area of interest (``search_area``), ``places_within_area`` takes that area the
same way. Each hop is a data key a model reads off a ``[state updated: ...]``
breadcrumb and passes back as ``@state:<key>``, so the geometries move between
tools through state, never through the model — see the runtime's
SESSION-STATE.md for the contract.

These tools query the same locked-down connection as ``tools.py``, but their
SQL is code-authored and parameter-bound, never caller text, so they skip
``validate_select_only`` and run under the longer ``LOOKUP_TIMEOUT_SECONDS``
budget the remote Overture scan needs (timings in ``connection.py``).
"""

import json
import logging
from typing import Annotated, Any, NotRequired

import duckdb
from langchain_core.tools import tool
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from mcp_runtime.declarations import NotAuthored
from mcp_runtime.tool_result import ToolError, ToolResult

from duckdb_analyst.connection import execute_capped
from duckdb_analyst.security import LOOKUP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

#: Fuzzy-match floor for ``get_place``. Probed against the 2026-07-22.0
#: release: real matches ("Time Out Market" → "Time Out Market Lisboa")
#: score ≥ 0.9, while jaro-winkler is generous enough on short names that
#: pure gibberish scored 0.75 against a four-letter venue — so the floor
#: sits above that failure, below real matches.
_SIMILARITY_FLOOR = 0.8

#: Cap on ``get_search_area``'s buffer. The area feeds a bbox-pruned remote
#: scan in ``places_within_area``; the probe put a ~10 km-wide bbox at
#: ~160s, near the LOOKUP_TIMEOUT_SECONDS ceiling, so anything much larger
#: is a guaranteed timeout rather than a bigger answer.
_MAX_BUFFER_KM = 25.0

_PLACES_IN_AREA_LIMIT = 100


class GetPlaceResult(ToolResult):
    """The best-matching Overture place, published for downstream tools."""

    place: NotRequired[dict]


class SearchAreaResult(ToolResult):
    """A buffered area of interest around a previously found place."""

    search_area: NotRequired[dict]


class PlacesWithinAreaResult(ToolResult):
    """Places of one category inside the area, as a FeatureCollection."""

    places: NotRequired[dict]


#: Common phrasings → Overture category slugs, carried over from
#: geo-assistant. Unknown inputs pass through lowercased, so any real
#: Overture slug works untranslated.
_CATEGORY_ALIASES = {
    "restaurants": "restaurant",
    "cafes": "cafe",
    "coffee": "cafe",
    "coffee shop": "cafe",
    "coffee shops": "cafe",
    "coffeeshop": "cafe",
    "bars": "bar",
    "pubs": "bar",
    "pub": "bar",
}


def _feature(geometry_geojson: str, properties: dict[str, Any]) -> dict[str, Any]:
    """A GeoJSON Feature from DuckDB's ST_AsGeoJSON output and properties."""
    return {
        "type": "Feature",
        "geometry": json.loads(geometry_geojson),
        "properties": properties,
    }


def _validate_bbox(bbox: list[float]) -> str | None:
    """An error detail if ``bbox`` is not a usable [west, south, east, north]."""
    if len(bbox) != 4:
        return "search_bbox must be [west, south, east, north]"
    west, south, east, north = bbox
    if not (west < east and south < north):
        return "search_bbox is empty: west must be < east and south < north"
    if not (-180 <= west and east <= 180 and -90 <= south and north <= 90):
        return "search_bbox is out of range for EPSG:4326 degrees"
    return None


def _json_list(raw: Any) -> list[Any]:
    """A JSON-encoded DuckDB list column as a Python list (never None)."""
    if raw is None:
        return []
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return parsed if isinstance(parsed, list) else []


@tool
async def get_place(
    place_name: str, search_bbox: list[float]
) -> GetPlaceResult | ToolError:
    """Find one place (a POI: a venue, shop, restaurant, landmark) in
    Overture Maps by fuzzy name match, within a bounding box.

    `search_bbox` is [west, south, east, north] in degrees and bounds a
    REMOTE scan — keep it as tight as you can name (a neighborhood beats a
    city; a whole country will time out). The matched place is published to
    session state for `get_search_area` to buffer around.
    """
    logger.debug("get_place: name=%r search_bbox=%s", place_name, search_bbox)
    if detail := _validate_bbox(search_bbox):
        logger.info("get_place rejected bbox %s: %s", search_bbox, detail)
        return ToolError(error="invalid_bbox", detail=detail)
    west, south, east, north = search_bbox

    sql = """
        SELECT
            id,
            names.primary AS name,
            confidence,
            categories.primary AS category,
            CAST(websites AS JSON) AS websites,
            CAST(socials AS JSON) AS socials,
            ST_AsGeoJSON(geometry) AS geometry,
            jaro_winkler_similarity(LOWER(names.primary), LOWER(?)) AS similarity
        FROM overture_places
        WHERE bbox.xmin > ? AND bbox.xmax < ?
          AND bbox.ymin > ? AND bbox.ymax < ?
          AND jaro_winkler_similarity(LOWER(names.primary), LOWER(?)) > ?
        ORDER BY similarity DESC
        LIMIT 1
    """
    params = [
        place_name,
        west,
        east,
        south,
        north,
        place_name,
        _SIMILARITY_FLOOR,
    ]
    try:
        _columns, rows = await execute_capped(
            sql, params, timeout=LOOKUP_TIMEOUT_SECONDS
        )
    except duckdb.Error as error:
        if isinstance(error, duckdb.InterruptException):
            return ToolError(
                error="timeout",
                detail="The Overture scan timed out — pass a tighter search_bbox.",
            )
        logger.warning("get_place query failed: %s", error)
        return ToolError(error="query_failed", detail=str(error))

    if not rows:
        logger.info("get_place: no match for %r in bbox %s", place_name, search_bbox)
        return ToolError(
            error="not_found",
            detail=f"No Overture place matches {place_name!r} in that bbox. "
            "Try a different spelling or a larger (but still tight) bbox.",
        )

    place_id, name, confidence, category, websites, socials, geometry, score = rows[0]
    place = _feature(
        geometry,
        {
            "overture_id": place_id,
            "name": name,
            "confidence": confidence,
            "category": category,
            "websites": _json_list(websites),
            "socials": _json_list(socials),
        },
    )
    logger.debug(
        "get_place: matched %r (id=%s, category=%s, similarity=%.2f)",
        name,
        place_id,
        category,
        score,
    )
    described = f"{name!r} ({category})" if category else f"{name!r}"
    return GetPlaceResult(
        message=f"Found {described}, name similarity {score:.2f}.",
        place=place,
    )


@tool
def get_search_area(
    place: Annotated[dict, NotAuthored()], buffer_km: float = 0.25
) -> SearchAreaResult | ToolError:
    """Buffer the previously found place by a radius in km, publishing the
    result as the area of interest for `places_within_area` (or any other
    tool that takes one).

    Omit `buffer_km` to use 0.25 km. Pass a value only when the question
    names a distance or the thing to show is larger than about 500 m across:
    a larger area makes a slower place scan and a coarser aerial image
    (0.25 km renders at 1 m/pixel, 1 km at about 4 m/pixel). It is capped at
    25 km, as the area drives a remote Overture scan and a larger one is a
    guaranteed timeout rather than a bigger answer.
    """
    logger.debug("get_search_area: buffer_km=%s", buffer_km)
    if not 0 < buffer_km <= _MAX_BUFFER_KM:
        logger.info("get_search_area rejected buffer_km=%s", buffer_km)
        return ToolError(
            error="invalid_buffer",
            detail=f"buffer_km must be in (0, {_MAX_BUFFER_KM}] — the search "
            "area feeds a remote scan that cannot cover more.",
        )
    geometry = place.get("geometry") if isinstance(place, dict) else None
    if not geometry:
        return ToolError(
            error="invalid_place",
            detail="place has no geometry — run get_place first.",
        )

    point = shape(geometry)
    # Buffer in a local azimuthal-equidistant frame centered on the place, so
    # the radius is true meters at any latitude (geo-assistant buffered in
    # Web Mercator, which inflates with latitude).
    center = point.representative_point()
    local = f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +units=m"
    to_local = Transformer.from_crs("EPSG:4326", local, always_xy=True)
    to_wgs84 = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
    buffered = transform(
        to_wgs84.transform,
        transform(to_local.transform, point).buffer(buffer_km * 1000),
    )

    properties = dict(place.get("properties") or {})
    properties["buffer_km"] = buffer_km
    search_area = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(buffered),
                "properties": properties,
            }
        ],
    }
    name = properties.get("name", "the place")
    logger.debug(
        "get_search_area: %s km around %r, area bounds %s",
        buffer_km,
        name,
        buffered.bounds,
    )
    return SearchAreaResult(
        message=f"Search area created: {buffer_km} km around {name}.",
        search_area=search_area,
    )


@tool
async def places_within_area(
    category: str,
    area: Annotated[dict, NotAuthored()],
    limit: int = 10,
) -> PlacesWithinAreaResult | ToolError:
    """List Overture places of one category (e.g. 'restaurant', 'cafe',
    'bar' — any Overture category slug) inside the current area of interest.

    The area comes from session state (published by `get_search_area`, or
    any tool publishing an area of interest). Returns up to `limit` places
    (max 100) as a GeoJSON FeatureCollection plus a readable list, which the
    user sees on a map. `category` is exactly one slug: there is no wildcard.
    To count or compare categories in an area, `query` `overture_places`
    and GROUP BY `categories.primary` instead. SQL cannot read session
    state, so filter its `bbox` columns on a box of numbers around the
    place's coordinates (from `inspect_state` of the place).
    """
    requested = category
    category = _CATEGORY_ALIASES.get(category.lower().strip(), category.lower().strip())
    limit = max(1, min(limit, _PLACES_IN_AREA_LIMIT))
    logger.debug(
        "places_within_area: category=%r (from %r), limit=%d",
        category,
        requested,
        limit,
    )

    features = (
        area.get("features", [])
        if isinstance(area, dict) and area.get("type") == "FeatureCollection"
        else [area]
    )
    try:
        geometry = unary_union([shape(feature["geometry"]) for feature in features])
    except (KeyError, TypeError, AttributeError, ValueError):
        logger.info("places_within_area: area is not usable GeoJSON")
        return ToolError(
            error="invalid_area",
            detail="area is not GeoJSON — run get_search_area first.",
        )
    west, south, east, north = geometry.bounds
    logger.debug(
        "places_within_area: scanning bbox (%.4f, %.4f, %.4f, %.4f)",
        west,
        south,
        east,
        north,
    )

    sql = """
        SELECT
            id,
            names.primary AS name,
            categories.primary AS category,
            CAST(websites AS JSON) AS websites,
            ST_AsGeoJSON(geometry) AS geometry
        FROM overture_places
        WHERE bbox.xmin > ? AND bbox.xmax < ?
          AND bbox.ymin > ? AND bbox.ymax < ?
          AND categories.primary = ?
          AND ST_Intersects(geometry, ST_GeomFromGeoJSON(?))
        LIMIT ?
    """
    params = [
        west,
        east,
        south,
        north,
        category,
        json.dumps(mapping(geometry)),
        # One past the limit, dropped below: it tells a full page from a
        # truncated one, so "10 places" is never reported as the total.
        limit + 1,
    ]
    try:
        _columns, rows = await execute_capped(
            sql, params, timeout=LOOKUP_TIMEOUT_SECONDS
        )
    except duckdb.Error as error:
        if isinstance(error, duckdb.InterruptException):
            return ToolError(
                error="timeout",
                detail="The Overture scan timed out — use a smaller search area.",
            )
        logger.warning("places_within_area query failed: %s", error)
        return ToolError(error="query_failed", detail=str(error))
    truncated = len(rows) > limit
    rows = rows[:limit]

    collection: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            _feature(
                geometry_geojson,
                {
                    "overture_id": place_id,
                    "name": name,
                    "category": row_category,
                    "websites": _json_list(websites),
                },
            )
            for place_id, name, row_category, websites, geometry_geojson in rows
        ],
    }
    if not rows:
        return PlacesWithinAreaResult(
            message=f"No {category!r} places found in the search area.",
            places=collection,
        )
    lines = []
    for feature in collection["features"]:
        properties = feature["properties"]
        website = next(iter(properties["websites"]), None)
        suffix = f" - {website}" if website else ""
        lines.append(f"  • {properties['name']}{suffix}")
    listing = "\n".join(lines)
    found = (
        f"Showing the first {len(rows)} {category!r} places; more exist in the "
        "area, so this is not the total"
        if truncated
        else f"Found {len(rows)} {category!r} place(s), the complete set in the area"
    )
    return PlacesWithinAreaResult(
        message=f"{found}:\n{listing}",
        places=collection,
    )


GEO_TOOLS = [get_place, get_search_area, places_within_area]
