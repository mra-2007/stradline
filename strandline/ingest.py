"""Download Sentinel-1 backscatter and the Copernicus DEM via openEO
(Copernicus Data Space), onto ONE fixed analysis grid, with caching.

Processing (calibration, orthorectification with the DEM) happens on the
Copernicus servers; only the cropped area is downloaded. Every file is
cached, so reruns and the live demo never re-download the same thing.

Every array is finally re-projected locally onto the exact same grid, so
pre, post and DEM pixels line up perfectly even if the backend output is
off by a pixel.
"""

import math
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

from strandline.catalog import Acquisition
from strandline.config import BACKSCATTER_COEFFICIENTS, ELEVATION_MODELS

OPENEO_BACKEND = "openeo.dataspace.copernicus.eu"


@dataclass(frozen=True)
class Grid:
    epsg: int
    res: float
    west: float
    south: float
    east: float
    north: float

    @property
    def width(self) -> int:
        return int(round((self.east - self.west) / self.res))

    @property
    def height(self) -> int:
        return int(round((self.north - self.south) / self.res))

    @property
    def transform(self) -> Affine:
        return from_origin(self.west, self.north, self.res, self.res)

    @property
    def crs(self) -> CRS:
        return CRS.from_epsg(self.epsg)

    def openeo_extent(self) -> dict:
        return {"west": self.west, "south": self.south, "east": self.east,
                "north": self.north, "crs": self.epsg}


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180) / 6)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def make_grid(bbox_ll: tuple[float, float, float, float], res: float) -> Grid:
    w, s, e, n = bbox_ll
    epsg = utm_epsg((w + e) / 2, (s + n) / 2)
    x0, y0, x1, y1 = transform_bounds("EPSG:4326", f"EPSG:{epsg}", w, s, e, n, densify_pts=21)
    snap = lambda v, f: f(v / res) * res  # noqa: E731
    return Grid(epsg, res, snap(x0, math.floor), snap(y0, math.floor),
                snap(x1, math.ceil), snap(y1, math.ceil))


def read_on_grid(path: Path, grid: Grid, resampling=Resampling.bilinear) -> np.ndarray:
    """Read every band of `path`, re-projected exactly onto `grid` (NaN = no data)."""
    with rasterio.open(path) as src:
        out = np.full((src.count, grid.height, grid.width), np.nan, dtype=np.float32)
        for b in range(1, src.count + 1):
            reproject(
                source=rasterio.band(src, b), destination=out[b - 1],
                src_nodata=src.nodata, dst_nodata=np.nan,
                dst_transform=grid.transform, dst_crs=grid.crs, resampling=resampling,
            )
    return out


# --- openEO ------------------------------------------------------------------

def connect(backend: str = OPENEO_BACKEND):
    import openeo
    return openeo.connect(backend).authenticate_oidc()


def _iso(t) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _download(cube, out_path: Path) -> Path:
    """Fast synchronous download; large areas fall back to a batch job."""
    tmp = out_path.with_suffix(".part.tif")
    try:
        cube.download(str(tmp), format="GTiff")
    except Exception as sync_error:  # timeouts / size limits on big areas
        print(f"      sync download failed ({type(sync_error).__name__}); running batch job ...")
        cube.execute_batch(outputfile=str(tmp), out_format="GTiff")
    tmp.replace(out_path)
    return out_path


def coefficient_slug(coefficient: str) -> str:
    """Filename-safe form of an openEO backscatter coefficient name."""
    if coefficient not in BACKSCATTER_COEFFICIENTS:
        raise ValueError(
            f"backscatter_coefficient {coefficient!r} is not supported; "
            f"choose one of {', '.join(BACKSCATTER_COEFFICIENTS)}."
        )
    return coefficient.replace("-", "_")


def elevation_model_slug(elevation_model: str) -> str:
    """Filename-safe form of the openEO elevation-model collection id."""
    if elevation_model not in ELEVATION_MODELS:
        raise ValueError(
            f"elevation_model {elevation_model!r} is not supported; "
            f"choose one of {', '.join(ELEVATION_MODELS)}."
        )
    return elevation_model.lower().replace("-", "_")


def s1_cache_name(track_tag: str, day, res: float, coefficient: str,
                  elevation_model: str) -> str:
    """Cache filename for one acquisition.

    The coefficient AND the elevation model are both part of the name on
    purpose. Each is a different measurement of the same scene: a coefficient
    changes the radiometric reference area, and the elevation model changes
    where every pixel is placed during orthorectification. A stack that
    silently mixed either would show change that came from the processing
    rather than from the flood. Both are therefore required arguments - there
    is no default that could quietly produce a mixed cache.
    """
    return (f"s1_{coefficient_slug(coefficient)}_{elevation_model_slug(elevation_model)}"
            f"_{track_tag}_{day}_{int(res)}m.tif")


def download_s1(conn, acq: Acquisition, grid: Grid, out_path: Path,
                coefficient: str, with_mask: bool,
                elevation_model: str = "COPERNICUS_30") -> Path:
    """One acquisition -> GeoTIFF with bands VV, VH (+ mask). Linear backscatter.

    `elevation_model` is passed to sar_backscatter explicitly. Left unset, the
    backend falls back to SRTMGL1, which has no data above 60 deg N and fails
    outright there (see strandline/config.py ELEVATION_MODELS).
    """
    coefficient_slug(coefficient)      # reject unsupported values before any
    elevation_model_slug(elevation_model)  # network call
    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Narrow time window around the exact pass keeps other tracks out.
    t0, t1 = acq.start - timedelta(minutes=2), acq.stop + timedelta(minutes=2)
    cube = conn.load_collection("SENTINEL1_GRD", spatial_extent=grid.openeo_extent(),
                                temporal_extent=[_iso(t0), _iso(t1)], bands=["VV", "VH"])
    cube = cube.sar_backscatter(coefficient=coefficient, mask=with_mask,
                                elevation_model=elevation_model)
    cube = cube.resample_spatial(resolution=grid.res, projection=grid.epsg, method="bilinear")
    cube = cube.reduce_dimension(dimension="t", reducer="mean")
    return _download(cube, out_path)


def download_dem(conn, grid: Grid, out_path: Path) -> Path:
    if out_path.exists():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cube = conn.load_collection("COPERNICUS_30", spatial_extent=grid.openeo_extent(), bands=["DEM"])
    if cube.metadata.has_temporal_dimension():
        cube = cube.reduce_dimension(dimension="t", reducer="max")
    cube = cube.resample_spatial(resolution=grid.res, projection=grid.epsg, method="bilinear")
    return _download(cube, out_path)


def looks_like_db(arr: np.ndarray) -> bool:
    """Backends return linear backscatter; guard against a dB surprise."""
    vals = arr[np.isfinite(arr)]
    return vals.size > 0 and float(np.median(vals)) < 0
