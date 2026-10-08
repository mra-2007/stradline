"""Compare our flood map against a reference map (e.g. Copernicus EMS EMSR927).

CHECKING ONLY. The pipeline (strandline/) must never import this module or
read reference data; tests/test_isolation.py enforces that. Using EMS maps
as input is grounds for disqualification.

    python -m validation.compare --pred out/flood_classes.tif --reference ems_observed_event.shp
        [--aoi ems_area_of_interest.shp] [--include-debris] [--tolerance-px 1] --out out/validation

Reports strict and boundary-tolerant precision, recall, F1 and IoU. The
tolerant score counts a pixel as correct if the other map has flood within
`tolerance-px` pixels: 10-20 m radar vs very-high-resolution reference maps
disagree on edges even when both are right.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from scipy import ndimage

from strandline.config import CLASS_DEBRIS, CLASS_NODATA, FLOOD_CLASSES

EIGHT = np.ones((3, 3), dtype=bool)
REFERENCE_CREDIT = "European Union, Copernicus Emergency Management Service data"


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def _f1(p, r):
    return round(2 * p * r / (p + r), 4) if p and r else 0.0


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


def _rasterize_vector(path: Path, like) -> np.ndarray:
    import geopandas as gpd
    gdf = gpd.read_file(path).to_crs(like.crs)
    geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
    return rasterize(((g, 1) for g in geoms), out_shape=(like.height, like.width),
                     transform=like.transform, fill=0, dtype="uint8").astype(bool)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--pred", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--aoi", type=Path, help="reference product's area of interest polygon")
    ap.add_argument("--include-debris", action="store_true")
    ap.add_argument("--tolerance-px", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    with rasterio.open(args.pred) as src:
        classes = src.read(1)
        ref = _rasterize_vector(args.reference, src)
        domain = classes != CLASS_NODATA
        if args.aoi:
            domain &= _rasterize_vector(args.aoi, src)

    flood_codes = list(FLOOD_CLASSES) + ([CLASS_DEBRIS] if args.include_debris else [])
    pred = np.isin(classes, flood_codes)
    metrics = flood_metrics(pred, ref, domain, args.tolerance_px)
    metrics.update({"include_debris": args.include_debris, "domain_pixels": int(domain.sum()),
                    "reference_credit": REFERENCE_CREDIT})

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
