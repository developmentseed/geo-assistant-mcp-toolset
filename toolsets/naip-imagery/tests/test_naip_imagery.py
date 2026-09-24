"""Tests for the NAIP imagery tools.

No network: the STAC search and raster load only run against the real
Planetary Computer, so `fetch_naip_image` is tested on its validation paths
and pure helpers, and `interpret_image` against a monkeypatched Ollama call.
The state-wiring test is the one to keep green above all: it pins the
cross-toolset contract — the area arrives from whatever published an
`area` (duckdb-analyst's `get_search_area` today) and the image moves to
`interpret_image` through session state, never through the model.
"""

import base64
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np
from pystac.item import Item
from shapely.geometry import box, mapping, shape

from mcp_runtime.declarations import not_authored, output_fields
from mcp_runtime.tool_result import is_error

import naip_imagery.tools as tools_module
from naip_imagery.tools import (
    _MAX_DIMENSION_PX,
    _MAX_MOSAIC_ITEMS,
    _area_geometry,
    _render_mosaic,
    _resolution_for,
    _stretch_to_uint8,
    fetch_naip_image,
    interpret_image,
)

# A ~2km square over Washington, DC — inside NAIP coverage.
DC_AREA: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [-77.05, 38.89],
                        [-77.03, 38.89],
                        [-77.03, 38.91],
                        [-77.05, 38.91],
                        [-77.05, 38.89],
                    ]
                ],
            },
            "properties": {},
        }
    ],
}


def test_state_wiring_links_area_to_image_to_interpretation():
    """get_search_area → fetch_naip_image → interpret_image, linked by state.

    These assertions are what makes the area and the ~100KB image flow
    through session state instead of the model's context. Dropping a
    `NotAuthored` tag or a result field silently degrades the chain to
    model-passed values, so any change here must be deliberate.
    """
    assert not_authored(fetch_naip_image) == ["area"]
    assert "naip_image" in output_fields(fetch_naip_image)
    assert not_authored(interpret_image) == ["image"]


def test_area_geometry_unions_features():
    geometry = _area_geometry(DC_AREA)
    assert geometry is not None
    assert geometry.equals(shape(DC_AREA["features"][0]["geometry"]))


def test_area_geometry_rejects_junk():
    assert _area_geometry({"type": "FeatureCollection", "features": [{}]}) is None


def test_resolution_native_for_small_areas():
    # ~2km across at 512px is ~4m — above native — while a 200m yard is
    # comfortably inside the pixel budget at native resolution.
    small = shape(
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [-77.001, 38.900],
                    [-77.000, 38.900],
                    [-77.000, 38.901],
                    [-77.001, 38.901],
                    [-77.001, 38.900],
                ]
            ],
        }
    )
    assert _resolution_for(small) == 1.0


def test_resolution_coarsens_to_fit_budget():
    geometry = _area_geometry(DC_AREA)
    assert geometry is not None
    resolution = _resolution_for(geometry)
    west, south, east, north = geometry.bounds
    span_m = (north - south) * 111_320
    assert resolution > 1.0
    assert span_m / resolution <= _MAX_DIMENSION_PX + 1


def test_stretch_to_uint8_full_range():
    arr = np.linspace(0, 1000, 300, dtype="float32").reshape(10, 10, 3)
    stretched = _stretch_to_uint8(arr)
    assert stretched.dtype == np.uint8
    assert stretched.min() == 0 and stretched.max() == 255


def test_stretch_to_uint8_constant_input():
    stretched = _stretch_to_uint8(np.full((4, 4, 3), 7.0))
    assert stretched.dtype == np.uint8


def _item(item_id: str, footprint: Any, day: int) -> Item:
    return Item(
        id=item_id,
        geometry=mapping(footprint),
        bbox=list(footprint.bounds),
        datetime=datetime(2023, 6, day, tzinfo=timezone.utc),
        properties={"proj:epsg": 32618},
    )


def _stub_load(monkeypatch, loads: list[str]) -> None:
    """Replace the raster read: each item fills the pixels its footprint's
    west-east span covers, with a value derived from its id."""

    def fake_load(item, geobox):
        loads.append(item.id)
        height, width = geobox.shape.yx
        west, _, east, _ = shape(item.geometry).bounds
        frame_west, _, frame_east, _ = geobox.geographic_extent.boundingbox
        span = frame_east - frame_west
        start = round((max(west, frame_west) - frame_west) / span * width)
        stop = round((min(east, frame_east) - frame_west) / span * width)
        rgb = np.full((height, width, 3), np.nan, dtype="float32")
        rgb[:, start:stop] = float(len(loads) * 50)
        return rgb

    monkeypatch.setattr(tools_module, "_load_rgb", fake_load)


def test_mosaic_fills_from_several_items(monkeypatch):
    area = box(-77.01, 38.90, -76.99, 38.91)
    west = _item("west", box(-77.02, 38.89, -77.00, 38.92), day=2)
    east = _item("east", box(-77.00, 38.89, -76.98, 38.92), day=1)
    loads: list[str] = []
    _stub_load(monkeypatch, loads)

    _, _, _, used, coverage = _render_mosaic([east, west], area, 10.0)

    assert [item.id for item in used] == ["west", "east"]  # newest first
    assert coverage > 0.99


def test_mosaic_stops_once_covered(monkeypatch):
    area = box(-77.01, 38.90, -76.99, 38.91)
    newest = _item("newest", box(-77.02, 38.89, -76.98, 38.92), day=3)
    older = _item("older", box(-77.02, 38.89, -76.98, 38.92), day=1)
    loads: list[str] = []
    _stub_load(monkeypatch, loads)

    _, _, _, used, coverage = _render_mosaic([older, newest], area, 10.0)

    assert loads == ["newest"] and coverage == 1.0


def test_mosaic_caps_item_count(monkeypatch):
    # Thin strips, so no small set of items covers the area.
    area = box(-77.01, 38.90, -76.99, 38.91)
    step = 0.02 / 20
    strips = [
        _item(
            f"s{i}",
            box(-77.01 + i * step, 38.89, -77.01 + (i + 1) * step, 38.92),
            day=i + 1,
        )
        for i in range(20)
    ]
    loads: list[str] = []
    _stub_load(monkeypatch, loads)

    _, _, _, used, coverage = _render_mosaic(strips, area, 10.0)

    assert len(loads) == _MAX_MOSAIC_ITEMS == len(used)
    assert coverage < 1.0


async def test_fetch_rejects_bad_dates():
    result = await fetch_naip_image.ainvoke(
        {"start_date": "not-a-date", "end_date": "2023-01-01", "area": DC_AREA}
    )
    assert is_error(result) and result["error"] == "invalid_dates"

    result = await fetch_naip_image.ainvoke(
        {"start_date": "2023-01-01", "end_date": "2020-01-01", "area": DC_AREA}
    )
    assert is_error(result) and result["error"] == "invalid_dates"


async def test_fetch_rejects_bad_area():
    result = await fetch_naip_image.ainvoke(
        {"start_date": "2020-01-01", "end_date": "2023-01-01", "area": {"nope": 1}}
    )
    assert is_error(result) and result["error"] == "invalid_area"


async def test_interpret_requires_image():
    result = await interpret_image.ainvoke({"image": {}})
    assert is_error(result) and result["error"] == "invalid_image"


async def test_interpret_returns_model_answer(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_generate(base_url, model, prompt, image_b64):
        seen.update(base_url=base_url, model=model, prompt=prompt, image=image_b64)
        return {"response": " A river crossing farmland. "}

    monkeypatch.setattr(tools_module, "_ollama_generate", fake_generate)
    encoded = base64.b64encode(b"jpegbytes").decode("ascii")
    result = await interpret_image.ainvoke(
        {"image": {"base64": encoded}, "question": "What is this?"}
    )
    assert not is_error(result)
    assert result["message"] == "A river crossing farmland."
    assert seen["image"] == encoded and seen["prompt"] == "What is this?"


async def test_interpret_reports_dead_ollama(monkeypatch):
    async def fake_generate(base_url, model, prompt, image_b64):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(tools_module, "_ollama_generate", fake_generate)
    result = await interpret_image.ainvoke({"image": {"base64": "aGk="}})
    assert is_error(result) and result["error"] == "ollama_unavailable"


async def test_interpret_reports_missing_model(monkeypatch):
    async def fake_generate(base_url, model, prompt, image_b64):
        request = httpx.Request("POST", "http://localhost:11434/api/generate")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(tools_module, "_ollama_generate", fake_generate)
    result = await interpret_image.ainvoke({"image": {"base64": "aGk="}})
    assert is_error(result) and result["error"] == "model_not_found"


async def test_interpret_reports_missing_signin(monkeypatch):
    # A -cloud model without `ollama signin` comes back as a 401 from the
    # local daemon.
    async def fake_generate(base_url, model, prompt, image_b64):
        request = httpx.Request("POST", "http://localhost:11434/api/generate")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(tools_module, "_ollama_generate", fake_generate)
    result = await interpret_image.ainvoke({"image": {"base64": "aGk="}})
    assert is_error(result) and result["error"] == "ollama_unauthorized"
