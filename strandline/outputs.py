"""Outputs: GeoTIFFs, flood polygons (GeoJSON), a quicklook PNG and a
provenance report. The report is the 'evidence' every later number
(dashboard, copilot, situation report) must trace back to."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from rasterio.features import shapes  # noqa: E402
from rasterio.warp import transform_geom  # noqa: E402
from shapely.geometry import shape  # noqa: E402

from strandline.config import (  # noqa: E402
    CLASS_NAMES,
    SOURCE_NAMES,
    NARROW_VALLEY_CAVEAT,
    RELIABILITY_NAMES,
    RELIABILITY_NARROW_VALLEY,
)

CLASS_COLORS = {
    0: "#000000", 1: "#d9d9d6", 2: "#1f5fbf", 3: "#7fb2e8",
    4: "#c0622f", 5: "#0b2e59", 6: "#9e9d97",
}

ATTRIBUTION = [
    "Contains modified Copernicus Sentinel data 2026.",
    "Produced using Copernicus WorldDEM-30 © DLR e.V. 2010–2014 and © Airbus Defence and "
    "Space GmbH 2014–2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.",
    "© OpenStreetMap contributors.",
]


def write_geotiff(path: Path, array: np.ndarray, grid, nodata=None) -> None:
    data = array[None] if array.ndim == 2 else array
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[1], width=data.shape[2],
                       count=data.shape[0], dtype=data.dtype, crs=grid.crs,
                       transform=grid.transform, nodata=nodata, compress="deflate") as dst:
        dst.write(data)


def class_areas_ha(classes: np.ndarray, pixel_size_m: float) -> dict[str, float]:
    px_ha = pixel_size_m * pixel_size_m / 10_000.0
    return {CLASS_NAMES[int(c)]: round(float(np.sum(classes == c)) * px_ha, 2)
            for c in np.unique(classes)}


def write_polygons(path: Path, classes: np.ndarray, grid, export=(2, 3, 4)) -> int:
    """Vectorise selected classes; area computed in UTM, geometry in EPSG:4326."""
    features = []
    mask = np.isin(classes, export)
    for geom, value in shapes(classes, mask=mask, transform=grid.transform, connectivity=8):
        v = int(value)
        area_ha = shape(geom).area / 10_000.0
        features.append({
            "type": "Feature",
            "geometry": transform_geom(grid.crs, "EPSG:4326", geom),
            "properties": {"class_code": v, "class_name": CLASS_NAMES[v],
                           "area_ha": round(area_ha, 3)},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return len(features)


def write_reliability_polygons(path: Path, codes: np.ndarray, grid) -> int:
    """Vectorise the narrow-valley zone as its own layer.

    A separate file from flood_polygons.geojson on purpose: this is a qualifier
    on the map, not a detection, and a consumer must be able to draw it (or not)
    without touching the flood layer. Every feature carries the caveat text, so
    a dashboard or a sitrep cannot render the geometry without the sentence.
    """
    features = []
    mask = codes == RELIABILITY_NARROW_VALLEY
    for geom, _ in shapes(codes, mask=mask, transform=grid.transform, connectivity=8):
        features.append({
            "type": "Feature",
            "geometry": transform_geom(grid.crs, "EPSG:4326", geom),
            "properties": {
                "reliability_code": int(RELIABILITY_NARROW_VALLEY),
                "reliability_name": RELIABILITY_NAMES[RELIABILITY_NARROW_VALLEY],
                "caveat": NARROW_VALLEY_CAVEAT,
                "area_ha": round(shape(geom).area / 10_000.0, 3),
            },
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return len(features)


def write_quicklook(path: Path, result, title: str) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(12, 11), constrained_layout=True)
    panels = [
        (result.pre_mean_vv_db, "Pre-event mean VV (dB)", "gray", -25, 0),
        (result.post_vv_db, "Post-event VV (dB)", "gray", -25, 0),
        (result.z_combined, "Change z-score (blue = darker, red = brighter)", "RdBu_r", -6, 6),
    ]
    for ax, (img, name, cmap, lo, hi) in zip(axes.flat[:3], panels):
        im = ax.imshow(img, cmap=cmap, vmin=lo, vmax=hi, interpolation="nearest")
        ax.set_title(name)
        ax.axis("off")
        fig.colorbar(im, ax=ax, shrink=0.7)
    cmap = ListedColormap([CLASS_COLORS[i] for i in range(7)])
    ax = axes.flat[3]
    ax.imshow(result.classes, cmap=cmap, vmin=-0.5, vmax=6.5, interpolation="nearest")
    ax.set_title("Classification")
    ax.axis("off")
    present = [int(c) for c in np.unique(result.classes)]
    ax.legend(handles=[Patch(color=CLASS_COLORS[c], label=CLASS_NAMES[c]) for c in present],
              loc="lower left", fontsize=8, framealpha=0.85)
    fig.suptitle(title)
    fig.text(0.5, 0.002, ATTRIBUTION[0], ha="center", fontsize=7)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def write_report(path: Path, report: dict) -> None:
    report = {**report, "attribution": ATTRIBUTION}
    path.write_text(json.dumps(report, indent=2, default=str))


def source_name(code) -> str:
    """Human-readable name of a per-pixel sensor-provenance code."""
    return SOURCE_NAMES[int(code)]
