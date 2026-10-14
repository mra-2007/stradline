"""Terrarium-encoded terrain tiles from a run's own cached Copernicus DEM.

The dashboard renders the valley in 3D. MapLibre's `raster-dem` source reads
elevation out of the RGB channels of a PNG, and the encoding it understands
without a plugin is Mapzen's *terrarium*:

    v = elevation_m + 32768
    R = v // 256
    G = v % 256
    B = round(fraction * 256)

so 0 m is (128, 0, 0) and one metre is one G step. We serve those tiles from
`runs/<run_id>/cache/dem_20m.tif` — the DEM the pipeline already downloaded and
resampled for this exact area (Rule 1: Copernicus DEM, no new source, no
network at request time). A tile that falls outside the DEM comes back flat at
0 m rather than as an error, because a hole in a terrain source makes MapLibre
tear the mesh.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

TILE_SIZE = 256

#: The DEM file inside a run directory, in the order worth trying.
DEM_CANDIDATES = ("cache/dem_20m.tif", "cache/dem.tif", "dem_20m.tif")

#: Shown next to the map whenever these tiles are on screen.
TERRAIN_ATTRIBUTION = "Copernicus DEM"


def find_dem(run_dir: Path) -> Path | None:
    """The run's own cached DEM, or None if this run never fetched one."""
    for name in DEM_CANDIDATES:
        candidate = Path(run_dir) / name
        if candidate.exists():
            return candidate
    return None


def encode_terrarium(elevation_m: np.ndarray) -> np.ndarray:
    """Elevation in metres -> a (h, w, 3) uint8 terrarium array.

    Anything not finite (the DEM's nodata is NaN) is encoded as 0 m, which is
    flat rather than a spike.
    """
    elevation = np.asarray(elevation_m, dtype="float64")
    elevation = np.where(np.isfinite(elevation), elevation, 0.0)
    # Terrarium covers -32768..32767 m; clip so a corrupt pixel cannot wrap
    # around into a mountain.
    shifted = np.clip(elevation + 32768.0, 0.0, 65535.999)
    whole = np.floor(shifted)
    red = np.floor(whole / 256.0)
    green = whole - red * 256.0
    blue = np.floor((shifted - whole) * 256.0)
    return np.stack([red, green, blue], axis=-1).astype("uint8")


def _to_png(rgb: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def flat_tile_png(tilesize: int = TILE_SIZE) -> bytes:
    """A tile that is 0 m everywhere: what we serve outside the DEM."""
    rgb = encode_terrarium(np.zeros((tilesize, tilesize), dtype="float32"))
    return _to_png(rgb)


def terrarium_tile_png(dem_path: Path, z: int, x: int, y: int,
                       tilesize: int = TILE_SIZE) -> bytes:
    """One terrarium PNG for the web-mercator tile (z, x, y).

    Outside the DEM's footprint this returns `flat_tile_png()` instead of
    raising, so the terrain mesh stays continuous at the edge of the area.
    """
    try:
        with Reader(str(dem_path)) as src:
            image = src.tile(x, y, z, tilesize=tilesize, resampling_method="bilinear")
    except TileOutsideBounds:
        return flat_tile_png(tilesize)

    elevation = image.data[0].astype("float64")
    if image.mask is not None:
        # rio-tiler masks are 255 where valid; treat the rest as 0 m.
        elevation = np.where(np.asarray(image.mask) > 0, elevation, 0.0)
    return _to_png(encode_terrarium(elevation))
