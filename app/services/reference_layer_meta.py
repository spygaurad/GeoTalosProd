"""What each public reference layer contains, and the style a user may set on it.

``LAYER_META`` is the descriptor the UI reads: geometry type, the attributes in the tiles
(label, unit, kind) and which numeric attribute can weight a heatmap. A new layer needs one
entry here and no UI change. ``density`` names the point tileset (one point per polygon,
built by scripts/reference_layers/build_pmtiles.sh) that draws the heatmap; layers without
it only have the box style.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _a(name: str, label: str, kind: str = "text", unit: str | None = None, weight: bool = False) -> dict:
    return {"name": name, "label": label, "kind": kind, "unit": unit, "weight": weight}


LAYER_META: dict[str, dict] = {
    "ramsar": {"geometry": "polygon", "attributes": [
        _a("officialna", "Name"), _a("country_en", "Country", "category"),
        _a("area_off", "Official area", "number", "ha")]},
    "cofopri": {"geometry": "polygon", "attributes": [
        _a("NOMBRE", "Name"), _a("NOMDEP", "Department", "category"), _a("TIPCOM", "Type", "category"),
        _a("AREHAS", "Area", "number", "ha")]},
    "raisg_protected_areas": {"geometry": "polygon", "attributes": [
        _a("nombre", "Name"), _a("pais", "Country", "category"), _a("categoria", "Category", "category"),
        _a("categoria_uicn", "IUCN category", "category"), _a("area_oficial_ha", "Official area", "number", "ha")]},
    "raisg_mining": {"geometry": "polygon", "density": "raisg_mining_density", "attributes": [
        _a("nombre", "Name"), _a("pais", "Country", "category"), _a("situacion", "Status", "category"),
        _a("tipo_minerio", "Mineral", "category"), _a("area_sig_ha", "Area", "number", "ha", weight=True)]},
    "gfw_mining": {"geometry": "polygon", "density": "gfw_mining_density", "attributes": [
        _a("conc_name", "Name"), _a("iso3", "Country", "category"), _a("conc_stat", "Status", "category"),
        _a("mineral", "Mineral", "category"), _a("company", "Company")]},
    "gfw_logging": {"geometry": "polygon", "attributes": [
        _a("name", "Name"), _a("country", "Country", "category"), _a("status", "Status", "category"),
        _a("area_ha", "Area", "number", "ha")]},
    "hydrorivers": {"geometry": "line", "attributes": [
        _a("ORD_STRA", "Strahler order", "number"), _a("DIS_AV_CMS", "Mean flow", "number", "m3/s"),
        _a("LENGTH_KM", "Length", "number", "km")]},
    "osm_roads": {"geometry": "line", "attributes": [
        _a("fclass", "Road class", "category"), _a("name", "Name"), _a("ref", "Reference"),
        _a("maxspeed", "Speed limit", "number", "km/h")]},
    "mrds": {"geometry": "point", "attributes": [
        _a("SITE_NAME", "Site"), _a("DEV_STAT", "Development status", "category"), _a("CODE_LIST", "Commodities")]},
}

# Point tilesets that draw the heatmaps. Served by Martin, never listed as layers of their own.
DENSITY_SOURCES: tuple[str, ...] = tuple(m["density"] for m in LAYER_META.values() if "density" in m)


class Heatmap(BaseModel):
    radius: int = Field(18, ge=4, le=60)
    weight: str | None = None  # attribute name; None counts every polygon as 1

    model_config = {"extra": "forbid"}


class LayerStyle(BaseModel):
    """Only the fields the user set are stored; omitted fields fall back to the UI default."""

    colour: str | None = None
    opacity: float | None = Field(None, ge=0, le=1)
    mode: Literal["boxes", "heatmap"] | None = None
    heatmap: Heatmap | None = None

    model_config = {"extra": "forbid"}

    @field_validator("colour")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("colour must look like #c0392b")
        return v


def check_style_for_layer(name: str, style: LayerStyle) -> None:
    """Raise ValueError when the style asks for something this layer cannot do."""
    meta = LAYER_META[name]
    if style.mode == "heatmap" and "density" not in meta:
        raise ValueError(f"{name} has no heatmap tiles")
    weight = style.heatmap.weight if style.heatmap else None
    if weight is not None and weight not in {a["name"] for a in meta["attributes"] if a["weight"]}:
        raise ValueError(f"{weight} cannot weight a heatmap on {name}")
