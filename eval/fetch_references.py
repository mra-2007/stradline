"""Download the scoring references for the dev events into data/reference/<event>/.

SCORING ONLY. Nothing here is pipeline input: `strandline/` never imports
`eval/` (enforced by the rules test), and nothing under data/reference/ is read
by the pipeline. This module only fetches reference maps so `validation/` can
score our output against them.

Usage
-----
    venv/Scripts/python.exe -m eval.fetch_references            # all flood events
    venv/Scripts/python.exe -m eval.fetch_references --event bosnia_konjic_2024
    venv/Scripts/python.exe -m eval.fetch_references --verify-only

Held-out safety (Rule 4)
------------------------
Every event name and every URL is checked against the forbidden held-out names
declared in the gates file. A match aborts the whole run rather than skipping
one item, because a held-out name turning up here means something upstream is
wrong. Downloads are cached by URL, so reruns are cheap and deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "eval" / "events.yaml"
GATES = ROOT / "eval" / "gates.yaml"
DEST = ROOT / "data" / "reference"
CACHE = ROOT / "data" / "_download_cache"

UA = {"User-Agent": "Mozilla/5.0 (Strandline reference fetcher)"}
TIMEOUT = 300

# Vector/raster extensions we expect a reference to arrive as.
SPATIAL = {".shp", ".geojson", ".json", ".gpkg", ".tif", ".tiff", ".kml", ".gml"}
# Sidecars a shapefile needs; .shp alone is unreadable.
SIDECARS = {".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix"}


class HeldOutViolation(RuntimeError):
    """A held-out name appeared in an event or a URL. Refuse, do not continue."""


def forbidden_names() -> list[str]:
    gates = yaml.safe_load(GATES.read_text(encoding="utf-8"))
    held = gates["heldout"]
    names = [str(n).lower() for n in held["forbidden_names"]]
    names.append(str(held["reference_activation"]).lower())
    names.append(str(held["name"]).lower())
    return sorted(set(names))


def guard(text: str, where: str, banned: list[str]) -> None:
    low = text.lower()
    hit = [n for n in banned if n in low]
    if hit:
        raise HeldOutViolation(
            f"held-out name {hit[0]!r} found in {where}: {text!r}. "
            "The case-study reference is sealed until the human unlocks it; refusing to fetch."
        )


@dataclass
class Event:
    name: str
    kind: str
    urls: list[str]
    reference: str
    independent: bool | None = None
    notes: list[str] = field(default_factory=list)


def load_events(only: str | None = None) -> list[Event]:
    banned = forbidden_names()
    raw = yaml.safe_load(EVENTS.read_text(encoding="utf-8")) or {}
    out = []
    for ev in raw.get("events", []):
        name = str(ev.get("name", ""))
        guard(name, "event name", banned)
        urls = list(ev.get("download_urls") or [])
        for u in urls:
            guard(u, f"download url for {name}", banned)
        if only and name != only:
            continue
        if not urls:
            continue  # controls have no reference by design
        out.append(Event(name=name, kind=str(ev.get("kind", "")), urls=urls,
                         reference=str(ev.get("reference", "")),
                         independent=ev.get("independent_reference")))
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str) -> tuple[Path, bool]:
    """Fetch `url` into the cache. Returns (path, was_cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    target = CACHE / f"{key}_{url.rsplit('/', 1)[-1].split('?')[0]}"
    if target.exists() and target.stat().st_size > 0:
        return target, True
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    if not data:
        raise RuntimeError(f"empty response from {url}")
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target, False


def _extract_flat(zip_path: Path, dest: Path) -> list[Path]:
    """Extract every member of `zip_path` into `dest`, discarding inner directories."""
    written = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/") or ".." in member or Path(member).is_absolute():
                continue
            flat = dest / Path(member).name
            with zf.open(member) as fh, flat.open("wb") as out:
                shutil.copyfileobj(fh, out)
            written.append(flat)
    return written


def unpack(src: Path, dest: Path, max_depth: int = 3) -> None:
    """Copy `src` into `dest`, expanding zips (including zips inside zips).

    Providers ship a zip of zips (JAXA here). Nested archives are expanded
    breadth-first from an explicit queue rather than by re-globbing `dest`:
    re-globbing sees archives a deeper call has already consumed and tries to
    delete them twice.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if not zipfile.is_zipfile(src):
        # Cache names are prefixed with a URL hash; restore the real filename.
        shutil.copyfile(src, dest / src.name.split("_", 1)[-1])
        return

    queue = [(src, 0)]
    seen: set[Path] = set()
    while queue:
        archive, depth = queue.pop(0)
        resolved = archive.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for produced in _extract_flat(archive, dest):
            if (produced.suffix.lower() == ".zip" and depth < max_depth
                    and produced.resolve() not in seen and zipfile.is_zipfile(produced)):
                queue.append((produced, depth + 1))
        if archive != src:
            archive.unlink(missing_ok=True)   # an inner archive, now expanded


def describe(path: Path) -> list[str]:
    """Open a spatial file and report layer, feature count, CRS and bounds."""
    import warnings
    warnings.filterwarnings("ignore")
    lines = []
    if path.suffix.lower() in {".tif", ".tiff"}:
        import rasterio
        with rasterio.open(path) as ds:
            lines.append(f"{path.name}: raster {ds.width}x{ds.height} bands={ds.count} "
                         f"crs={ds.crs} bounds={tuple(round(v, 4) for v in ds.bounds)}")
        return lines
    import geopandas as gpd
    try:
        import pyogrio
        layers = [str(n) for n in pyogrio.list_layers(path)[:, 0]]
    except Exception:
        layers = [path.stem]
    for layer in layers:
        try:
            g = gpd.read_file(path, layer=layer) if len(layers) > 1 else gpd.read_file(path)
        except Exception as err:
            lines.append(f"{path.name} [{layer}]: UNREADABLE ({type(err).__name__}: {err})")
            continue
        crs = g.crs.to_string() if g.crs is not None else "NO CRS"
        if g.empty:
            lines.append(f"{path.name} [{layer}]: n=0 crs={crs} EMPTY")
            continue
        b = g.total_bounds
        geom = ",".join(sorted(set(g.geom_type.dropna().astype(str))))
        attrs = [c for c in g.columns if c != g.geometry.name]
        lines.append(f"{path.name} [{layer}]: n={len(g)} {geom} crs={crs} "
                     f"bounds=[{b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f}] attrs={attrs}")
    return lines


def spatial_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*")
                  if p.is_file() and p.suffix.lower() in SPATIAL)


def check_shapefile_sidecars(d: Path) -> list[str]:
    problems = []
    for shp in d.rglob("*.shp"):
        missing = [e for e in (".shx", ".dbf") if not shp.with_suffix(e).exists()]
        if missing:
            problems.append(f"{shp.name}: missing required sidecar(s) {missing}")
    return problems


def fetch_event(ev: Event, verify_only: bool) -> dict:
    out = DEST / ev.name
    print(f"\n===== {ev.name}  ({ev.kind}, independent_reference={ev.independent})")
    record = {"event": ev.name, "kind": ev.kind, "independent_reference": ev.independent,
              "reference": ev.reference, "downloads": [], "layers": [], "problems": []}

    if not verify_only:
        for url in ev.urls:
            try:
                src, cached = download(url)
            except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as err:
                code = getattr(err, "code", None)
                msg = f"DOWNLOAD FAILED {url} -> {type(err).__name__} {code or err}"
                if code in (401, 402, 403):
                    msg += ("  <-- looks like a login / licence / captcha wall. "
                            "Open the URL in a browser, accept the licence, and place the "
                            f"file in {out} by hand; then rerun with --verify-only.")
                print("   " + msg)
                record["problems"].append(msg)
                continue
            size = src.stat().st_size
            digest = sha256(src)
            print(f"   {'cached ' if cached else 'fetched'} {size:>10,} B  sha256={digest[:16]}  {url}")
            record["downloads"].append({"url": url, "bytes": size, "sha256": digest})
            unpack(src, out)

    if not out.exists():
        record["problems"].append(f"no data directory {out}")
        print(f"   NOTHING ON DISK at {out}")
        return record

    files = spatial_files(out)
    if not files:
        record["problems"].append("no spatial file found after unpacking")
        print("   NO SPATIAL FILE FOUND after unpacking")
    for problem in check_shapefile_sidecars(out):
        record["problems"].append(problem)
        print("   PROBLEM: " + problem)
    for f in files:
        for line in describe(f):
            print("   " + line)
            record["layers"].append(line)
            if "UNREADABLE" in line or "NO CRS" in line:
                record["problems"].append(line)
    return record


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", help="fetch only this event")
    ap.add_argument("--verify-only", action="store_true",
                    help="do not download; just open and describe what is already on disk")
    args = ap.parse_args(argv)

    try:
        events = load_events(args.event)
    except HeldOutViolation as err:
        print(f"REFUSING: {err}", file=sys.stderr)
        return 3

    if not events:
        print("No events with download_urls matched.", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    records = [fetch_event(ev, args.verify_only) for ev in events]

    # Merge, never replace: `--event X` must not wipe the other events' records.
    manifest = DEST / "manifest.json"
    merged: dict[str, dict] = {}
    if manifest.exists():
        try:
            for old in json.loads(manifest.read_text(encoding="utf-8")):
                merged[old["event"]] = old
        except (ValueError, KeyError, TypeError):
            pass  # unreadable manifest: rebuild from what we just verified
    for r in records:
        merged[r["event"]] = r
    manifest.write_text(json.dumps([merged[k] for k in sorted(merged)], indent=1),
                        encoding="utf-8")

    bad = [r for r in records if r["problems"]]
    print(f"\n{len(records) - len(bad)}/{len(records)} events verified clean. "
          f"Manifest: {manifest.relative_to(ROOT)}")
    for r in bad:
        print(f"  ATTENTION {r['event']}: {len(r['problems'])} problem(s)")
        for p in r["problems"]:
            print(f"      - {p}")
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
