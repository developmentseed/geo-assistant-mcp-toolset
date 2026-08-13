"""The geo-assistant NAIP imagery workflow, as kind-tagged tools.

Two tools ported from geo-assistant's LangGraph agent: ``fetch_naip_image``
searches Microsoft Planetary Computer's STAC API for NAIP aerial imagery
over the session's area of interest and renders an RGB JPEG server-side;
``interpret_image`` sends that JPEG to a local Ollama vision model and
returns its description. The hops are ``Kind`` tags, so the area arrives
from ``duckdb-analyst``'s ``get_search_area`` (or any tool publishing an
area of interest) and the image moves between the two tools through session
state — never through the chat model's context, which matters here more
than for geometries: a 512px JPEG is ~100KB of base64.

The interpretation model is deliberately separate from the chat model: it is
an Ollama endpoint (``OLLAMA_BASE_URL``, default localhost), so the localhost
demo interprets imagery through its own Ollama daemon — by default a
``-cloud`` model that executes on ollama.com (no GPU needed), or any locally
pulled vision model via ``OLLAMA_IMAGE_MODEL`` — regardless of which
provider drives the chat.

Where the original aborted on rasters larger than 512x512, this port picks
the load resolution from the area's size instead, so any area the upstream
buffer cap allows renders — coarser, never bigger.
"""

import asyncio
import base64
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from typing import Annotated, Any, NotRequired, TypedDict

import httpx
import numpy as np
import planetary_computer
import xarray as xr
from langchain_core.tools import tool
from odc.geo.geom import Geometry
from odc.stac import stac_load
from PIL import Image
from pystac.extensions.raster import RasterBand
from pystac.item import Item
from pystac_client import Client
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from mcp_runtime.declarations import Kind
from mcp_runtime.kinds import GEOJSON_AREA_OF_INTEREST
from mcp_runtime.tool_result import ToolError, ToolResult

logger = logging.getLogger(__name__)

#: A base64-encoded JPEG with its provenance, shaped like ``NaipImage``.
#: Minted here because the runtime's vocabulary has no kind for a rendered
#: image yet — kinds are just strings, so producer and consumer agreeing on
#: this text is the whole contract (worth upstreaming into
#: ``mcp_runtime.kinds`` by PR, like ``geojson.PlaceFeature``).
IMAGE_JPEG_BASE64 = "image.JpegBase64"

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

#: NAIP's native ground resolution — the finest the load ever requests.
_NATIVE_RESOLUTION_M = 1.0

#: Longest edge of the rendered JPEG. The load resolution is chosen so the
#: raster fits this budget, whatever the area's size — the image feeds a
#: vision model and a chat view, neither of which wants more pixels.
_MAX_DIMENSION_PX = 512

#: Meters per degree of latitude (and of longitude at the equator).
_M_PER_DEGREE = 111_320.0

_OLLAMA_DEFAULT_URL = "http://localhost:11434"

#: A ``-cloud`` model executes on ollama.com through the local daemon
#: (``ollama signin`` once, then ``ollama pull`` fetches a stub), so the
#: demo needs no GPU — carried over from geo-assistant. Point
#: ``OLLAMA_IMAGE_MODEL`` at any installed vision model to run locally.
_OLLAMA_DEFAULT_MODEL = "gemma4:cloud"

#: Local vision inference on CPU can take minutes for a first (model-load)
#: call; connecting to a dead endpoint should still fail fast.
_OLLAMA_TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class NaipImage(TypedDict):
    """A rendered NAIP RGB composite plus where and when it came from."""

    media_type: str
    base64: str
    width: int
    height: int
    item_id: str
    item_datetime: str
    resolution_m: float


class FetchNaipImageResult(ToolResult):
    """The rendered NAIP JPEG, published for `interpret_image` and the view."""

    naip_image: NotRequired[Annotated[NaipImage, Kind(IMAGE_JPEG_BASE64)]]


class InterpretImageResult(ToolResult):
    """A vision model's reading of the fetched image."""

    model: NotRequired[str]


def _area_geometry(area: dict) -> BaseGeometry | None:
    """The union of an area's features as shapely, or None if unusable."""
    features = (
        area.get("features", [])
        if isinstance(area, dict) and area.get("type") == "FeatureCollection"
        else [area]
    )
    try:
        return unary_union([shape(feature["geometry"]) for feature in features])
    except (KeyError, TypeError, AttributeError, ValueError):
        return None


def _resolution_for(geometry: BaseGeometry) -> float:
    """The coarsest of native resolution and what fits the pixel budget."""
    west, south, east, north = geometry.bounds
    mid_lat = math.radians((south + north) / 2)
    span_m = max(
        (east - west) * _M_PER_DEGREE * math.cos(mid_lat),
        (north - south) * _M_PER_DEGREE,
    )
    return max(_NATIVE_RESOLUTION_M, span_m / _MAX_DIMENSION_PX)


def _stretch_to_uint8(arr: np.ndarray) -> np.ndarray:
    """A (y, x, band) float array as uint8 via a robust 2-98% stretch."""
    arr = arr.astype("float32")
    vmin = float(np.nanpercentile(arr, 2))
    vmax = float(np.nanpercentile(arr, 98))
    if vmax <= vmin:
        vmin, vmax = float(np.nanmin(arr)), float(np.nanmax(arr))
    arr = np.clip((arr - vmin) / (vmax - vmin + 1e-6), 0, 1)
    return (arr * 255).astype("uint8")


def _item_crs(item: Item) -> str:
    """The item's projection, whichever projection-extension field carries it."""
    code = item.properties.get("proj:code")
    if code:
        return str(code)
    return f"EPSG:{item.properties['proj:epsg']}"


def _render_rgb_jpeg(
    item: Item, geometry: BaseGeometry, resolution: float
) -> tuple[str, int, int]:
    """Load one item's RGB over the geometry and encode it as base64 JPEG.

    Blocking (rasterio reads); callers run it in a thread.
    """
    # Planetary Computer describes NAIP's bands with the eo:bands extension,
    # but odc.stac needs raster:bands to see the multi-band asset — carried
    # over from geo-assistant.
    item.assets["image"].ext.add("raster")
    item.assets["image"].ext.raster.bands = [
        RasterBand.create() for _ in ("red", "green", "blue", "nir")
    ]

    with ThreadPoolExecutor(max_workers=5) as executor:
        ds: xr.Dataset = stac_load(
            [item],
            bands=["red", "green", "blue"],
            geopolygon=Geometry(mapping(geometry), "EPSG:4326"),
            resolution=resolution,
            executor=executor,
            crs=_item_crs(item),
        )

    rgb = xr.concat(
        [ds["red"].isel(time=0), ds["green"].isel(time=0), ds["blue"].isel(time=0)],
        dim="band",
    ).transpose("y", "x", "band")
    arr = _stretch_to_uint8(rgb.values)

    height, width = arr.shape[:2]
    buf = BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=85)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return encoded, width, height


@tool
async def fetch_naip_image(
    start_date: str,
    end_date: str,
    area: Annotated[dict, Kind(GEOJSON_AREA_OF_INTEREST)],
) -> FetchNaipImageResult | ToolError:
    """Fetch NAIP aerial imagery (USA only, ~1m resolution) over the current
    area of interest and render it as an RGB image.

    Searches Microsoft Planetary Computer for NAIP acquisitions between
    `start_date` and `end_date` (YYYY-MM-DD; NAIP flies each state every 2-3
    years, so span a few years). The area comes from session state (published
    by `get_search_area`, or any tool publishing an area of interest). The
    rendered image is published for `interpret_image` to describe.
    """
    logger.debug("fetch_naip_image: %s/%s", start_date, end_date)
    try:
        if date.fromisoformat(start_date) > date.fromisoformat(end_date):
            return ToolError(
                error="invalid_dates", detail="start_date must be <= end_date."
            )
    except ValueError as error:
        return ToolError(error="invalid_dates", detail=str(error))

    geometry = _area_geometry(area)
    if geometry is None:
        logger.info("fetch_naip_image: area is not usable GeoJSON")
        return ToolError(
            error="invalid_area",
            detail="area is not GeoJSON — run get_search_area first.",
        )

    def search() -> list[Item]:
        # sign_inplace adds the SAS token NAIP's blob storage requires —
        # anonymous and free, no account involved.
        catalog = Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
        found = catalog.search(
            collections=["naip"],
            intersects=mapping(geometry),
            datetime=f"{start_date}/{end_date}",
        )
        return list(found.items())

    try:
        items = await asyncio.to_thread(search)
    except Exception as error:
        logger.warning("fetch_naip_image: STAC search failed: %s", error)
        return ToolError(error="stac_search_failed", detail=str(error))
    logger.debug("fetch_naip_image: STAC search returned %d item(s)", len(items))
    if not items:
        return ToolError(
            error="not_found",
            detail="No NAIP imagery for that area and date range. NAIP covers "
            "the USA only, revisiting each state every 2-3 years — widen the "
            "date range, or check the area is in the USA.",
        )

    # Newest acquisition wins; one item is enough for a preview (mosaicking
    # across flight lines is out of scope, as it was in geo-assistant).
    item = max(items, key=lambda entry: str(entry.datetime))
    resolution = _resolution_for(geometry)
    try:
        encoded, width, height = await asyncio.to_thread(
            _render_rgb_jpeg, item, geometry, resolution
        )
    except Exception as error:
        logger.warning("fetch_naip_image: raster load failed: %s", error)
        return ToolError(error="load_failed", detail=str(error))

    item_datetime = str(item.datetime.date()) if item.datetime else "unknown date"
    logger.debug(
        "fetch_naip_image: rendered %s (%s) as %dx%d at %.1f m/px",
        item.id,
        item_datetime,
        width,
        height,
        resolution,
    )
    return FetchNaipImageResult(
        message=f"Rendered NAIP acquisition {item.id} ({item_datetime}) as a "
        f"{width}x{height} RGB image at {resolution:.1f} m/pixel "
        f"({len(items)} acquisition(s) matched). Call interpret_image to "
        "describe it.",
        naip_image=NaipImage(
            media_type="image/jpeg",
            base64=encoded,
            width=width,
            height=height,
            item_id=item.id,
            item_datetime=item_datetime,
            resolution_m=round(resolution, 2),
        ),
    )


@tool
async def interpret_image(
    image: Annotated[dict, Kind(IMAGE_JPEG_BASE64)],
    question: str = "Describe what you see in this aerial image.",
) -> InterpretImageResult | ToolError:
    """Describe the previously fetched aerial image with an Ollama-served
    vision model.

    The image comes from session state (published by `fetch_naip_image`) and
    goes to an Ollama endpoint — `OLLAMA_BASE_URL` (default localhost:11434)
    running `OLLAMA_IMAGE_MODEL` — so it never enters this conversation's
    context. Ask a specific `question` to steer the description.
    """
    encoded = image.get("base64") if isinstance(image, dict) else None
    if not encoded:
        return ToolError(
            error="invalid_image",
            detail="No image available — run fetch_naip_image first.",
        )

    base_url = os.environ.get("OLLAMA_BASE_URL", _OLLAMA_DEFAULT_URL).rstrip("/")
    model = os.environ.get("OLLAMA_IMAGE_MODEL", _OLLAMA_DEFAULT_MODEL)
    logger.debug("interpret_image: model=%r at %s", model, base_url)

    try:
        payload = await _ollama_generate(base_url, model, question, encoded)
    except httpx.ConnectError:
        return ToolError(
            error="ollama_unavailable",
            detail=f"No Ollama at {base_url} — start it (`ollama serve`) or "
            "point OLLAMA_BASE_URL at one.",
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return ToolError(
                error="model_not_found",
                detail=f"Ollama has no model {model!r} — `ollama pull {model}` "
                "(`ollama signin` first for -cloud models), or set "
                "OLLAMA_IMAGE_MODEL to an installed vision model.",
            )
        if error.response.status_code == 401:
            return ToolError(
                error="ollama_unauthorized",
                detail=f"Ollama refused to run {model!r}: -cloud models "
                "execute on ollama.com, which needs `ollama signin` once on "
                "the machine running the daemon.",
            )
        return ToolError(error="ollama_error", detail=str(error))
    except httpx.HTTPError as error:
        return ToolError(error="ollama_error", detail=str(error))

    answer = str(payload.get("response", "")).strip()
    if not answer:
        return ToolError(
            error="empty_response",
            detail=f"{model!r} returned no text — is it a vision model?",
        )
    logger.debug("interpret_image: %d chars from %r", len(answer), model)
    return InterpretImageResult(message=answer, model=model)


async def _ollama_generate(
    base_url: str, model: str, prompt: str, image_b64: str
) -> dict[str, Any]:
    """One non-streaming Ollama generate call with a single image attached."""
    async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as client:
        response = await client.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
            },
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


TOOLS = [fetch_naip_image, interpret_image]

# The image view renders the fetched JPEG inline in MCP Apps hosts (and the
# bundled Chainlit agent) — without it the image exists only in session state.
VIEWS = {"fetch_naip_image": "image"}
