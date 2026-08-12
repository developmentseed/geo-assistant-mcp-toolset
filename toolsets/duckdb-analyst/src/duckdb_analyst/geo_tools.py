"""The geo-assistant workflow, as kind-tagged tools over Overture places.

Three tools ported from geo-assistant's LangGraph agent, whose state flow
(place → search area → places within it) maps directly onto session state:
``get_place`` publishes the matched place feature, ``get_search_area``
consumes it and publishes an area of interest, ``places_within_area``
consumes that area. Each hop is a ``Kind`` tag, so the geometries move
between tools through state with a receipt, never through the model — see
the runtime's SESSION-STATE.md for the contract.

These tools query the same locked-down connection as ``tools.py``, but their
SQL is code-authored and parameter-bound, never caller text, so they skip
``validate_select_only`` and run under the longer ``LOOKUP_TIMEOUT_SECONDS``
budget the remote Overture scan needs (timings in ``connection.py``).
"""

import json
from typing import Annotated, Any, NotRequired

import duckdb
from langchain_core.tools import tool
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from mcp_runtime.declarations import Kind
from mcp_runtime.kinds import GEOJSON_AREA_OF_INTEREST
from mcp_runtime.tool_result import ToolError, ToolResult

from duckdb_analyst.connection import execute_capped
from duckdb_analyst.security import LOOKUP_TIMEOUT_SECONDS

#: A single GeoJSON Feature for one named place (a POI, not an area). Minted
#: here because the runtime's vocabulary has no kind for it yet — kinds are
#: just strings, so producer and consumer agreeing on this text is the whole
#: contract. Worth upstreaming into ``mcp_runtime.kinds`` by PR so other
#: toolsets can interoperate with it.
GEOJSON_PLACE_FEATURE = "geojson.PlaceFeature"

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

    place: NotRequired[Annotated[dict, Kind(GEOJSON_PLACE_FEATURE)]]


class SearchAreaResult(ToolResult):
    """A buffered area of interest around a previously found place."""

    search_area: NotRequired[Annotated[dict, Kind(GEOJSON_AREA_OF_INTEREST)]]


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
    if detail := _validate_bbox(search_bbox):
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
        return ToolError(error="query_failed", detail=str(error))

    if not rows:
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
    described = f"{name!r} ({category})" if category else f"{name!r}"
    return GetPlaceResult(
        message=f"Found {described}, name similarity {score:.2f}.",
        place=place,
    )


@tool
def get_search_area(
    buffer_km: float, place: Annotated[dict, Kind(GEOJSON_PLACE_FEATURE)]
) -> SearchAreaResult | ToolError:
    """Buffer the previously found place by a radius in km, publishing the
    result as the area of interest for `places_within_area` (or any other
    tool that takes one).

    `buffer_km` is capped at 25 km: the area drives a remote Overture scan,
    and a larger one is a guaranteed timeout rather than a bigger answer.
    """
    if not 0 < buffer_km <= _MAX_BUFFER_KM:
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
    return SearchAreaResult(
        message=f"Search area created: {buffer_km} km around {name}.",
        search_area=search_area,
    )


@tool
async def places_within_area(
    category: str,
    area: Annotated[dict, Kind(GEOJSON_AREA_OF_INTEREST)],
    limit: int = 10,
) -> PlacesWithinAreaResult | ToolError:
    """List Overture places of one category (e.g. 'restaurant', 'cafe',
    'bar' — any Overture category slug) inside the current area of interest.

    The area comes from session state (published by `get_search_area`, or
    any tool publishing an area of interest). Returns up to `limit` places
    (max 100) as a GeoJSON FeatureCollection plus a readable list.
    """
    category = _CATEGORY_ALIASES.get(category.lower().strip(), category.lower().strip())
    limit = max(1, min(limit, _PLACES_IN_AREA_LIMIT))

    features = (
        area.get("features", [])
        if isinstance(area, dict) and area.get("type") == "FeatureCollection"
        else [area]
    )
    try:
        geometry = unary_union([shape(feature["geometry"]) for feature in features])
    except (KeyError, TypeError, AttributeError, ValueError):
        return ToolError(
            error="invalid_area",
            detail="area is not GeoJSON — run get_search_area first.",
        )
    west, south, east, north = geometry.bounds

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
        limit,
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
        return ToolError(error="query_failed", detail=str(error))

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
    return PlacesWithinAreaResult(
        message=f"Found {len(rows)} {category!r} place(s):\n{listing}",
        places=collection,
    )


GEO_TOOLS = [get_place, get_search_area, places_within_area]
