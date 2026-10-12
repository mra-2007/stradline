"""Compare our flood map against a reference map (e.g. Copernicus EMS EMSR927).

CHECKING ONLY. The pipeline (strandline/) must never import this module or
read reference data; tests/test_isolation.py enforces that. Using EMS maps
as input is grounds for disqualification.

    python -m validation.compare --pred out/flood_classes.tif --reference ems_observed_event.shp
        [--aoi ems_area_of_interest.shp] [--classes "Flooded area"] [--include-debris]
        [--tolerance-px 1] --out out/validation

Reports strict and boundary-tolerant precision, recall, F1 and IoU. The
tolerant score counts a pixel as correct if the other map has flood within
`tolerance-px` pixels: 10-20 m radar vs very-high-resolution reference maps
disagree on edges even when both are right.

Every metric definition lives here and nowhere else. `eval/run_scoreboard.py`
and `eval/tune.py` call these functions rather than reimplementing them, so
"what is measured" cannot drift between a manual check and a scoreboard row
(Rule 3.2).
"""

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from scipy import ndimage

from strandline.config import (
    CLASS_DEBRIS,
    CLASS_NODATA,
    FLOOD_AFFECTED_CLASSES,
    FLOOD_CLASSES,
    FLOOD_EXTENT_WITHOUT_DEBRIS,
)

EIGHT = np.ones((3, 3), dtype=bool)
REFERENCE_CREDIT = "European Union, Copernicus Emergency Management Service data"

# `reference_classes` value meaning "this layer has no attribute to filter on;
# every polygon in it belongs to the class". Used by the two references that
# ship no usable attribute columns at all.
ALL_POLYGONS = "__all__"


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _f1(p, r):
    return round(2 * p * r / (p + r), 4) if p and r else 0.0


# --- What counts as flood, and where we are allowed to be wrong --------------

def flood_mask(classes: np.ndarray, include_debris: bool = False) -> np.ndarray:
    """Our flood prediction: the flood classes, optionally plus debris."""
    codes = list(FLOOD_CLASSES) + ([CLASS_DEBRIS] if include_debris else [])
    return np.isin(classes, codes)


def flood_affected_mask(classes: np.ndarray, include_debris: bool = True) -> np.ndarray:
    """THE PRIMARY SCORED QUANTITY: flood-affected area.

    Water (either confidence) OR debris, because Track B asks for flooded and
    debris-covered areas. Scored against the union of each reference's flooded
    and debris/mass-movement classes. Decided and recorded in
    docs/tuning_log.md on 2026-10-11, before the new detection mode was scored.

    `include_debris=False` is ExtentParams.debris_in_flood_extent = False: the
    debris class stays in the class map but is not flood EXTENT, so it is not
    scored. The REFERENCE side does not follow this flag - references are still
    the union of flooded and debris/mass-movement - which makes it the harder
    option, by design (docs/tuning_log.md, 2026-10-12).
    """
    codes = FLOOD_AFFECTED_CLASSES if include_debris else FLOOD_EXTENT_WITHOUT_DEBRIS
    return np.isin(classes, list(codes))


def reference_class_values(reference_classes: dict, *keys: str) -> list[str]:
    """The declared attribute values for one or more reference classes.

    `reference_classes` comes straight from the event's entry in events.yaml;
    passing ("water", "debris") gives the union used by the primary metric.
    Order is preserved and duplicates dropped, so the result is deterministic.
    """
    out: list[str] = []
    for key in keys:
        for value in (reference_classes or {}).get(key) or []:
            if str(value) not in out:
                out.append(str(value))
    return out


def scoring_domain(classes: np.ndarray, aoi: np.ndarray | None = None) -> np.ndarray:
    """Every pixel the comparison is scored over.

    That is every pixel the pipeline actually analysed - which INCLUDES
    terrain-excluded ground, because a reference flood there is a miss we
    must be charged for, and excludes only true no-data. Narrowing this is
    exactly the "shrinking the scoring area" that Rule 3.2 forbids, so the
    pixel count is reported next to every score.
    """
    domain = classes != CLASS_NODATA
    return domain if aoi is None else (domain & aoi)


def false_flood_fraction(pred: np.ndarray, domain: np.ndarray) -> float | None:
    """The no-flood control metric: share of the analysed area called flood."""
    return _ratio(int(np.sum(pred & domain)), int(domain.sum()))


def flood_metrics(pred: np.ndarray, ref: np.ndarray, domain: np.ndarray, tolerance_px: int = 1) -> dict:
    pred, ref = pred & domain, ref & domain
    tp = int(np.sum(pred & ref))
    fp = int(np.sum(pred & ~ref))
    fn = int(np.sum(~pred & ref))
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    out = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
           "f1": _f1(precision, recall), "iou": _ratio(tp, tp + fp + fn)}
    if tolerance_px > 0:
        ref_near = ndimage.binary_dilation(ref, EIGHT, iterations=tolerance_px)
        pred_near = ndimage.binary_dilation(pred, EIGHT, iterations=tolerance_px)
        p_tol = _ratio(int(np.sum(pred & ref_near)), int(pred.sum()))
        r_tol = _ratio(int(np.sum(ref & pred_near)), int(ref.sum()))
        out.update({"tolerance_px": tolerance_px, "precision_tolerant": p_tol,
                    "recall_tolerant": r_tol, "f1_tolerant": _f1(p_tol, r_tol)})
    return out


# --- Reading a reference layer -----------------------------------------------

class ReferenceClassError(ValueError):
    """The declared reference_classes do not match the layer on disk.

    Raised rather than returning an empty mask: an empty reference would score
    as recall 0 / precision 0 and look like a detector failure, when it is
    really a mapping typo. Refuse and say which values the layer does have.
    """


@dataclass
class ReferenceSelection:
    """Provenance of one reference mask: what was asked for, what matched."""

    path: str
    values: list[str]
    n_total: int
    n_selected: int
    matched_columns: dict[str, int] = field(default_factory=dict)
    all_polygons: bool = False

    def describe(self) -> str:
        if self.all_polygons:
            how = "every polygon (layer has no attribute to filter on)"
        else:
            how = ", ".join(f"{c}={n}" for c, n in sorted(self.matched_columns.items()))
        return f"{self.n_selected}/{self.n_total} polygons by {how}"


def read_reference(path: Path):
    import geopandas as gpd
    return gpd.read_file(path)


def select_polygons(gdf, values) -> tuple[object, ReferenceSelection]:
    """Rows of `gdf` whose class is one of `values`, per events.yaml.

    `values` holds ATTRIBUTE VALUES, not column names: the reference products
    disagree on which column carries the class (`notation` on most Copernicus
    EMS delineations, `event_type` where flood and mass movement share a
    layer, `interpret` in Kuro Siwo), and events.yaml records only the values.
    So every column is searched and a polygon is selected when any of its
    attributes equals a declared value. The matched columns are reported, so
    a value that lands in an unexpected column is visible rather than silent.
    """
    values = [str(v) for v in (values or [])]
    sel = ReferenceSelection(path="", values=values, n_total=int(len(gdf)), n_selected=0)
    if not values:
        return gdf.iloc[0:0], sel
    if ALL_POLYGONS in values:
        sel.all_polygons = True
        sel.n_selected = int(len(gdf))
        return gdf, sel

    wanted = set(values)
    keep = np.zeros(len(gdf), dtype=bool)
    for col in gdf.columns:
        if col == gdf.geometry.name:
            continue
        hit = gdf[col].astype("string").isin(wanted).to_numpy(dtype=bool, na_value=False)
        if hit.any():
            sel.matched_columns[str(col)] = int(hit.sum())
            keep |= hit
    if not keep.any():
        seen = {str(v) for col in gdf.columns if col != gdf.geometry.name
                for v in gdf[col].astype("string").dropna().unique()[:40]}
        near = sorted(v for v in seen if any(v.lower() == w.lower() for w in wanted))
        hint = (f" Did you mean {near}? (case differs)" if near
                else f" Values present include: {sorted(seen)[:25]}")
        raise ReferenceClassError(
            f"reference_classes {values} match no polygon in this layer.{hint}")
    sel.n_selected = int(keep.sum())
    return gdf[keep], sel


def rasterize_geoms(geoms, like) -> np.ndarray:
    """Burn geometries onto `like` (anything with crs/transform/width/height)."""
    shapes = [(g, 1) for g in geoms if g is not None and not g.is_empty]
    if not shapes:
        return np.zeros((like.height, like.width), dtype=bool)
    return rasterize(shapes, out_shape=(like.height, like.width), transform=like.transform,
                     fill=0, dtype="uint8", all_touched=False).astype(bool)


def reference_mask(path: Path, like, values=(ALL_POLYGONS,)) -> tuple[np.ndarray, ReferenceSelection]:
    """One reference class, rasterised onto the analysis grid."""
    gdf = read_reference(path)
    chosen, sel = select_polygons(gdf, values)
    sel.path = str(path)
    if len(chosen) == 0:
        return np.zeros((like.height, like.width), dtype=bool), sel
    return rasterize_geoms(chosen.to_crs(like.crs).geometry, like), sel


def _rasterize_vector(path: Path, like) -> np.ndarray:
    """Every polygon of a layer (used for an area-of-interest outline)."""
    return reference_mask(path, like, (ALL_POLYGONS,))[0]


# --- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--pred", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--aoi", type=Path, help="reference product's area of interest polygon")
    ap.add_argument("--classes", nargs="*", default=[ALL_POLYGONS],
                    help=f"reference_classes values (default: {ALL_POLYGONS})")
    ap.add_argument("--include-debris", action="store_true")
    ap.add_argument("--tolerance-px", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    with rasterio.open(args.pred) as src:
        classes = src.read(1)
        ref, selection = reference_mask(args.reference, src, args.classes)
        aoi = _rasterize_vector(args.aoi, src) if args.aoi else None
        domain = scoring_domain(classes, aoi)

    pred = flood_mask(classes, args.include_debris)
    metrics = flood_metrics(pred, ref, domain, args.tolerance_px)
    metrics.update({"include_debris": args.include_debris, "domain_pixels": int(domain.sum()),
                    "domain_fraction": round(float(domain.mean()), 4),
                    "false_flood_fraction": false_flood_fraction(pred, domain),
                    "reference_selection": asdict(selection),
                    "reference_credit": REFERENCE_CREDIT})
    print(f"reference: {selection.describe()}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    err = np.zeros(classes.shape + (3,), dtype=np.float32)
    err[domain] = 0.92
    err[pred & ref & domain] = (0.15, 0.65, 0.25)    # agree: green
    err[pred & ~ref & domain] = (0.85, 0.2, 0.2)     # false alarm: red
    err[~pred & ref & domain] = (0.2, 0.35, 0.9)     # missed: blue
    plt.figure(figsize=(10, 10))
    plt.imshow(err, interpolation="nearest")
    plt.title(f"green = agree, red = false alarm, blue = missed | F1 {metrics['f1']}")
    plt.axis("off")
    plt.savefig(args.out / "error_map.png", dpi=110, bbox_inches="tight")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
