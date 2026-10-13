"""Who is cut off: settlements, road graphs, routes, and how sure we are.

Two graphs are built from the pre-event OpenStreetMap snapshot - what a vehicle
can drive and what a person can walk - and the edges the flood reached are
removed. Every settlement is then routed to its nearest health facility and its
nearest town, before and after, under two scenarios (bridges intact, bridges
failed), and the result is a status plus a probability.

THE ONE DEFINITION THE GATE USES

`config.CUTOFF_GATE_*`: a settlement is CUT OFF when it has no VEHICLE route to
its nearest health facility with bridges assumed intact. Vehicle because that is
what an ambulance needs, health facility because that is what a cut-off village
is cut off from, bridges intact because it is the conservative reading and the
failed-bridge case is a what-if reported beside it. Every other combination of
graph, destination and scenario is computed and written out, so a reader is
never stuck with the one summary.

"CUT OFF" AND "NOT MAPPED" ARE DIFFERENT ANSWERS

A settlement with no road in OSM, or none that reaches a destination even with
no flood at all, is reported `STATUS_NO_BASELINE` - never `cut off`. The flood
did not isolate it; we cannot say anything about it. Collapsing the two would
manufacture a flood impact out of a mapping gap, and the damage result already
showed how thin OSM can be (65 buildings mapped across the whole Larkana area).

WHY EDGES ARE TESTED AGAINST A RASTER, NOT A POLYGON

`strandline.damage` asks whether a road intersects the flood polygon, which is
the right test for one answer. This module needs the same question answered
~200 times over perturbed extents, and re-polygonising a mask and re-intersecting
tens of thousands of segments that many times is not tractable. So every edge is
sampled at half-pixel spacing ONCE into flat raster indices, and "is this edge
flooded" becomes an array lookup. The two tests agree on hand-made geometry
(tests/test_network.py), and the rasterised one is used for BOTH the headline
status and the ensemble so a settlement's status and its probability can never
come from different definitions.

THE PROBABILITY IS A SENSITIVITY MEASURE, NOT A CALIBRATED CHANCE

See `config.MonteCarloParams`. It is the share of a 200-run ensemble - the
change-score threshold perturbed by +-0.5 z, the road buffer over 0/1/2 px, and
every low-confidence narrow-valley pixel flooded on a coin flip - in which the
settlement comes out cut off. Nothing here has been validated against observed
cut-off outcomes, so 0.8 means "this answer survives most perturbations", not
"80% chance of being isolated". Every output repeats that.

No downloads: this module reads arrays and cached OSM only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from strandline.config import (
    CLASS_FLOOD_HIGH,
    CLASS_FLOOD_MEDIUM,
    CLASS_NO_CHANGE,
    CUTOFF_GATE_GRAPH,
    CUTOFF_GATE_SCENARIO,
    CUTOFF_GATE_TARGET,
    FOOT_EXTRA_HIGHWAY_TYPES,
    GRAPH_FOOT,
    GRAPH_KINDS,
    GRAPH_VEHICLE,
    RELIABILITY_NARROW_VALLEY,
    SCENARIO_BRIDGES_FAILED,
    SCENARIO_BRIDGES_INTACT,
    SCENARIOS,
    SETTLEMENT_PLACE_TYPES,
    STATUS_CUT_OFF,
    STATUS_DETOUR,
    STATUS_NO_BASELINE,
    STATUS_REACHABLE,
    TOWN_PLACE_TYPES,
    VEHICLE_HIGHWAY_TYPES,
    DamageParams,
    ExtentParams,
    MonteCarloParams,
    NetworkParams,
)

EIGHT = np.ones((3, 3), dtype=bool)
TARGETS = ("health", "town")

CUTOFF_CAVEAT = (
    "Cut-off status is computed from this run's own flood extent and a pre-event "
    "OpenStreetMap road network. It inherits the flood map's accuracy and OSM's "
    "completeness: a settlement with no mapped road is reported as having no baseline "
    "route, never as cut off. The probability is the share of a perturbed ensemble, a "
    "sensitivity measure - NOT a calibrated chance of isolation. Educational prototype."
)


class NetworkError(RuntimeError):
    """The cut-off analysis cannot be done here. Says what is missing."""


# --- the graph ----------------------------------------------------------------

@dataclass
class RoadGraph:
    """An undirected road graph with everything the routing needs.

    `edge_pixels` / `edge_pixel_offsets` hold, per edge, the flat raster indices
    the edge passes through, concatenated. That is what makes an ensemble of 200
    perturbed extents affordable (see the module docstring).
    """

    kind: str
    nodes: np.ndarray                 # (n_nodes, 2) projected x, y
    edge_u: np.ndarray                # (n_edges,) node index
    edge_v: np.ndarray
    edge_len: np.ndarray              # metres
    edge_is_bridge: np.ndarray        # bool
    edge_segment_id: list
    edge_pixels: np.ndarray           # flat raster indices, concatenated
    edge_pixel_offsets: np.ndarray    # (n_edges + 1,) into edge_pixels

    @property
    def n_nodes(self) -> int:
        return int(len(self.nodes))

    @property
    def n_edges(self) -> int:
        return int(len(self.edge_u))

    def total_length_m(self) -> float:
        return float(self.edge_len.sum())

    def flooded_edges(self, flat_mask: np.ndarray) -> np.ndarray:
        """Which edges touch a flood mask, given as a FLAT boolean raster.

        One pass over the concatenated pixel indices, reduced per edge. An edge
        with no sampled pixel (a zero-length fragment) is never flooded.
        """
        if self.n_edges == 0:
            return np.zeros(0, dtype=bool)
        hit = flat_mask[self.edge_pixels]
        counts = np.diff(self.edge_pixel_offsets)
        out = np.zeros(self.n_edges, dtype=bool)
        nonempty = counts > 0
        if nonempty.any():
            sums = np.add.reduceat(hit.astype(np.int64),
                                   self.edge_pixel_offsets[:-1][nonempty])
            out[nonempty] = sums > 0
        return out


def highway_types(kind: str) -> tuple:
    """Which OSM `highway` values belong to this graph."""
    if kind == GRAPH_VEHICLE:
        return tuple(VEHICLE_HIGHWAY_TYPES)
    if kind == GRAPH_FOOT:
        return tuple(VEHICLE_HIGHWAY_TYPES) + tuple(FOOT_EXTRA_HIGHWAY_TYPES)
    raise NetworkError(f"unknown graph kind {kind!r}; declared kinds are {GRAPH_KINDS}")


def _sample_pixels(geom, grid, step_m: float) -> np.ndarray:
    """Flat raster indices the line passes through, sampled every `step_m`.

    Half-pixel sampling cannot skip a cell a straight segment crosses, because
    a cell is at least `res` across in both directions. Out-of-raster samples
    are dropped: a road outside the mapped area cannot be flooded by this map.
    """
    length = float(geom.length)
    n = max(int(np.ceil(length / step_m)) + 1, 2)
    distances = np.linspace(0.0, length, n)
    xs = np.empty(n)
    ys = np.empty(n)
    for i, d in enumerate(distances):
        point = geom.interpolate(d)
        xs[i], ys[i] = point.x, point.y
    # The inverse transform applied explicitly: `affine * (array, array)` works
    # but is deprecated, and this runs once per edge.
    inv = ~grid.transform
    cols = np.floor(inv.a * xs + inv.b * ys + inv.c).astype(np.int64)
    rows = np.floor(inv.d * xs + inv.e * ys + inv.f).astype(np.int64)
    inside = (rows >= 0) & (rows < grid.height) & (cols >= 0) & (cols < grid.width)
    flat = rows[inside] * grid.width + cols[inside]
    return np.unique(flat)


def build_graph(segments, grid, kind: str, params: NetworkParams | None = None) -> RoadGraph:
    """One graph from already-split road segments (see damage.split_at_junctions).

    Splitting first is what makes the graph a graph: an OSM way is an editing
    unit that can run for kilometres past a dozen junctions, so using ways as
    edges would route traffic through junctions that do not exist.
    """
    np_ = params if params is not None else NetworkParams()
    wanted = set(highway_types(kind))
    if len(segments) and "highway" in segments.columns:
        keep = segments["highway"].astype("string").isin(wanted).to_numpy(
            dtype=bool, na_value=False)
        chosen = segments[keep]
    else:
        chosen = segments.iloc[0:0]

    coords: list[tuple[float, float]] = []
    ends: list[tuple[int, int]] = []
    lengths: list[float] = []
    bridges: list[bool] = []
    ids: list = []
    pixel_chunks: list[np.ndarray] = []
    step = float(grid.res) / 2.0

    def node_key(x: float, y: float) -> tuple[int, int]:
        snap = np_.node_snap_m
        return (int(round(x / snap)), int(round(y / snap)))

    index: dict[tuple[int, int], int] = {}

    def node_index(x: float, y: float) -> int:
        key = node_key(x, y)
        if key not in index:
            index[key] = len(coords)
            coords.append((x, y))
        return index[key]

    bridge_col = ("bridge" in chosen.columns)
    for i in range(len(chosen)):
        row = chosen.iloc[i]
        geom = row.geometry
        if geom is None or geom.is_empty or geom.length <= 0:
            continue
        parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            if part.is_empty or part.length <= 0:
                continue
            (x0, y0), (x1, y1) = part.coords[0], part.coords[-1]
            u, v = node_index(x0, y0), node_index(x1, y1)
            if u == v:
                continue        # a closed loop carries no connectivity
            ends.append((u, v))
            lengths.append(float(part.length))
            value = row["bridge"] if bridge_col else None
            bridges.append(bool(value is not None and str(value) not in ("nan", "no", "")))
            ids.append(row["segment_id"] if "segment_id" in chosen.columns else f"edge/{i}")
            pixel_chunks.append(_sample_pixels(part, grid, step))

    offsets = np.zeros(len(pixel_chunks) + 1, dtype=np.int64)
    if pixel_chunks:
        offsets[1:] = np.cumsum([len(c) for c in pixel_chunks])
    return RoadGraph(
        kind=kind,
        nodes=np.asarray(coords, dtype=float).reshape(-1, 2),
        edge_u=np.asarray([e[0] for e in ends], dtype=np.int64),
        edge_v=np.asarray([e[1] for e in ends], dtype=np.int64),
        edge_len=np.asarray(lengths, dtype=float),
        edge_is_bridge=np.asarray(bridges, dtype=bool),
        edge_segment_id=ids,
        edge_pixels=(np.concatenate(pixel_chunks) if pixel_chunks
                     else np.zeros(0, dtype=np.int64)),
        edge_pixel_offsets=offsets,
    )


def _matrix(graph: RoadGraph, removed: np.ndarray):
    """A symmetric sparse weight matrix with `removed` edges left out."""
    keep = ~removed
    u, v = graph.edge_u[keep], graph.edge_v[keep]
    w = graph.edge_len[keep]
    n = max(graph.n_nodes, 1)
    return coo_matrix((np.concatenate([w, w]),
                       (np.concatenate([u, v]), np.concatenate([v, u]))),
                      shape=(n, n)).tocsr()


def distance_to_nearest(graph: RoadGraph, sources: np.ndarray,
                        removed: np.ndarray) -> np.ndarray:
    """Shortest distance from every node to the NEAREST of `sources`, in metres.

    One multi-source Dijkstra rather than one per destination: the question is
    always "the nearest", never "this particular one". `inf` means no route.
    """
    if graph.n_nodes == 0 or len(sources) == 0:
        return np.full(max(graph.n_nodes, 0), np.inf)
    dist = dijkstra(_matrix(graph, removed), directed=False,
                    indices=np.unique(np.asarray(sources, dtype=np.int64)),
                    min_only=True)
    return np.asarray(dist, dtype=float)


# --- settlements ---------------------------------------------------------------

def dbscan(points: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """DBSCAN labels for 2-D points; -1 is noise. Deterministic.

    Written here on a KD-tree rather than taken from scikit-learn, which is not
    a dependency of this project. Standard definition: a point with at least
    `min_samples` neighbours within `eps` (itself included) is a core point,
    core points within `eps` of each other share a cluster, and a non-core point
    within `eps` of a core point joins that core's cluster. Cluster ids are
    assigned in order of first appearance, so the labelling is stable.
    """
    n = len(points)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    tree = cKDTree(points)
    neighbours = tree.query_ball_point(points, eps)
    counts = np.array([len(x) for x in neighbours])
    core = counts >= min_samples
    labels = np.full(n, -1, dtype=np.int64)
    if not core.any():
        return labels
    core_idx = np.flatnonzero(core)
    position = {int(idx): k for k, idx in enumerate(core_idx)}
    rows, cols = [], []
    for idx in core_idx:
        for other in neighbours[idx]:
            if core[other]:
                rows.append(position[int(idx)])
                cols.append(position[int(other)])
    graph = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)),
                       shape=(len(core_idx), len(core_idx))).tocsr()
    _n, comp = connected_components(graph, directed=False)
    # Renumber so cluster 0 is the one containing the first core point, etc.
    order: dict[int, int] = {}
    for k in range(len(core_idx)):
        c = int(comp[k])
        if c not in order:
            order[c] = len(order)
        labels[core_idx[k]] = order[c]
    for i in range(n):
        if labels[i] != -1:
            continue
        for other in neighbours[i]:
            if core[other]:
                labels[i] = labels[other]
                break
    return labels


@dataclass
class Settlements:
    """Where people are, and where each one attaches to each graph."""

    names: list
    kinds: list                       # OSM place value, or "building_cluster"
    sources: list                     # "osm_place" | "building_cluster"
    xy: np.ndarray                    # (n, 2) projected
    n_buildings: list                 # for clusters; None for place nodes
    # Filled in by `assess`: how many settlements no graph could attach. A
    # DECLARED LIMITATION, counted rather than hidden - a settlement is joined
    # to the nearest graph NODE, and nodes are junctions and way ends, so one
    # sitting beside the middle of a long unsplit road can fall outside
    # `settlement_link_max_m` and be reported as having no baseline route. That
    # is the conservative direction (it never invents a cut-off) but it does
    # understate how many settlements the analysis could speak about, so the
    # number belongs in every report.
    not_attached: int = 0

    def __len__(self) -> int:
        return len(self.names)

    def report(self) -> dict:
        from collections import Counter

        return {"total": len(self),
                "by_source": dict(sorted(Counter(self.sources).items())),
                "by_kind": dict(sorted(Counter(self.kinds).items())),
                "not_attached_to_any_graph": int(self.not_attached),
                "attachment_note": ("a settlement is joined to the nearest graph node "
                                    "within settlement_link_max_m; one beside the middle "
                                    "of a long road can fall outside that and is then "
                                    "reported as having no baseline route, never as cut "
                                    "off")}


def find_settlements(places, buildings, params: NetworkParams | None = None,
                     log=print) -> Settlements:
    """OSM place nodes, plus building clusters for the hamlets OSM has not named.

    Both are needed and neither is enough: place nodes are authoritative but
    routinely absent for small mountain settlements, and a building cluster is
    a guess that a group of roofs is a place where people live. Clusters within
    `cluster_merge_distance_m` of a place node are dropped as the same place.
    """
    np_ = params if params is not None else NetworkParams()
    names, kinds, sources, xs, ys, counts = [], [], [], [], [], []

    wanted = set(SETTLEMENT_PLACE_TYPES)
    if len(places) and "place" in places.columns:
        for i in range(len(places)):
            row = places.iloc[i]
            place = row["place"]
            if place is None or str(place) not in wanted:
                continue
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            point = geom if geom.geom_type == "Point" else geom.centroid
            name = row["name"] if "name" in places.columns else None
            names.append(str(name) if name is not None and str(name) != "nan"
                         else f"unnamed {place}")
            kinds.append(str(place))
            sources.append("osm_place")
            xs.append(point.x)
            ys.append(point.y)
            counts.append(None)

    place_xy = np.column_stack([xs, ys]) if xs else np.zeros((0, 2))
    if len(buildings):
        centroids = np.column_stack([buildings.geometry.centroid.x.to_numpy(),
                                     buildings.geometry.centroid.y.to_numpy()])
        labels = dbscan(centroids, np_.cluster_eps_m, np_.cluster_min_buildings)
        tree = cKDTree(place_xy) if len(place_xy) else None
        n_clusters = int(labels.max()) + 1 if len(labels) and labels.max() >= 0 else 0
        for label in range(n_clusters):
            members = centroids[labels == label]
            centre = members.mean(axis=0)
            if tree is not None:
                near, _ = tree.query(centre, k=1,
                                     distance_upper_bound=np_.cluster_merge_distance_m)
                if np.isfinite(near):
                    continue      # already an OSM place node
            names.append(f"building cluster {label}")
            kinds.append("building_cluster")
            sources.append("building_cluster")
            xs.append(float(centre[0]))
            ys.append(float(centre[1]))
            counts.append(int(len(members)))

    out = Settlements(names=names, kinds=kinds, sources=sources,
                      xy=np.column_stack([xs, ys]) if xs else np.zeros((0, 2)),
                      n_buildings=counts)
    log(f"      settlements: {len(out)} "
        f"({sources.count('osm_place')} OSM place node(s), "
        f"{sources.count('building_cluster')} building cluster(s))")
    return out


def attach(points: np.ndarray, graph: RoadGraph, max_m: float) -> np.ndarray:
    """Nearest graph node per point, or -1 when none is within `max_m`."""
    if len(points) == 0 or graph.n_nodes == 0:
        return np.full(len(points), -1, dtype=np.int64)
    tree = cKDTree(graph.nodes)
    dist, idx = tree.query(points, k=1, distance_upper_bound=max_m)
    idx = np.asarray(idx, dtype=np.int64)
    idx[~np.isfinite(dist)] = -1
    idx[idx >= graph.n_nodes] = -1
    return idx


# --- the flood mask, and perturbed versions of it -----------------------------

def flood_flat_mask(classes: np.ndarray, grid, extent: ExtentParams | None = None,
                    buffer_px: int | None = None) -> np.ndarray:
    """The configured flood extent as a FLAT boolean raster, buffered.

    Deliberately the same classes and the same buffer as
    `strandline.damage.extent_mask`, so a road the damage layer calls cut and an
    edge this module removes are the same roads.
    """
    from strandline import damage

    return damage.extent_mask(classes, extent, buffer_px).ravel()


def perturbed_flat_mask(classes: np.ndarray, z_combined: np.ndarray,
                        reliability: np.ndarray | None, z_seed: float,
                        z_delta: float, buffer_px: int, narrow_flooded: np.ndarray | None,
                        extent: ExtentParams | None = None) -> np.ndarray:
    """One ensemble member's flood extent, as a flat boolean raster.

    ANCHORED on the extent the detector actually produced, and moved only where
    the cached change score justifies it:

      looser threshold (z_delta < 0)  the extent GROWS to include analysed,
                                     non-permanent ground whose |z| clears the
                                     lowered threshold;
      stricter threshold (z_delta > 0)  the extent SHRINKS to the pixels of
                                     itself whose |z| still clears the raised
                                     threshold.

    Anchoring matters: re-deriving an extent from the change score alone would
    throw away the magnitude gate, the hysteresis, the minimum mapping unit and
    the drainage test, so at zero perturbation it would NOT reproduce the map
    the run actually made - and an ensemble that does not contain the real
    answer cannot say how stable the real answer is. With `z_delta = 0` this
    function returns exactly the configured extent.

    `narrow_flooded` is the per-pixel coin flip for low-confidence
    narrow-valley cells, drawn by the caller so the seed governs it.
    """
    from strandline import damage

    base = damage.extent_mask(classes, extent, buffer_px=0)
    if z_delta < 0:
        # Ground the detector analysed and did not call permanent water: the
        # only place a looser threshold may add flood.
        candidate = np.isin(classes, (CLASS_NO_CHANGE, CLASS_FLOOD_HIGH, CLASS_FLOOD_MEDIUM))
        with np.errstate(invalid="ignore"):
            louder = candidate & (np.abs(z_combined) >= (z_seed + z_delta))
        mask = base | np.nan_to_num(louder, nan=False).astype(bool)
    elif z_delta > 0:
        with np.errstate(invalid="ignore"):
            survives = np.abs(z_combined) >= (z_seed + z_delta)
        mask = base & np.nan_to_num(survives, nan=False).astype(bool)
    else:
        mask = base.copy()

    if narrow_flooded is not None and reliability is not None:
        mask = mask | (narrow_flooded & (reliability == RELIABILITY_NARROW_VALLEY))
    if buffer_px > 0 and mask.any():
        mask = ndimage.binary_dilation(mask, EIGHT, iterations=int(buffer_px))
    return mask.ravel()


# --- routing one configuration ------------------------------------------------

def _removed(graph: RoadGraph, flooded: np.ndarray, scenario: str,
             bridge_out: np.ndarray | None) -> np.ndarray:
    """Which edges are gone: flooded ones, plus bridges in the failed scenario."""
    removed = flooded.copy()
    if scenario == SCENARIO_BRIDGES_FAILED:
        removed = removed | (bridge_out if bridge_out is not None else graph.edge_is_bridge)
    return removed


def statuses(before: np.ndarray, after: np.ndarray, nodes: np.ndarray,
             detour_factor: float) -> list:
    """Per settlement: reachable, detour, cut off, or no baseline route.

    `nodes` is the attachment node per settlement, -1 for unattached. The order
    of the tests is the whole content of this function: a settlement with no
    PRE-flood route is never called cut off, whatever the post-flood answer is.
    """
    out = []
    for i, node in enumerate(nodes):
        if node < 0 or not np.isfinite(before[node]):
            out.append(STATUS_NO_BASELINE)
            continue
        post = after[node]
        if not np.isfinite(post):
            out.append(STATUS_CUT_OFF)
        elif post > detour_factor * max(before[node], 1e-9):
            out.append(STATUS_DETOUR)
        else:
            out.append(STATUS_REACHABLE)
    return out


@dataclass
class CutoffResult:
    """Everything the cut-off analysis found, plus the report numbers."""

    settlements: Settlements
    graphs: dict                      # kind -> RoadGraph
    attached: dict                    # kind -> node index per settlement
    status: dict                      # (kind, target, scenario) -> list[str]
    before_m: dict                    # (kind, target) -> array per settlement
    after_m: dict                     # (kind, target, scenario) -> array
    probability: np.ndarray           # per settlement, the gate definition
    n_runs: int
    # (graph kind, destination) -> graph node indices of that destination, as
    # the routing actually used them. Kept so an evaluation can re-route the
    # SAME sources against a different extent and be certain the only thing
    # that differs is the extent.
    target_nodes: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    targets_found: dict = field(default_factory=dict)
    ensemble: dict = field(default_factory=dict)

    def gate_status(self) -> list:
        return self.status[(CUTOFF_GATE_GRAPH, CUTOFF_GATE_TARGET, CUTOFF_GATE_SCENARIO)]

    def cut_off_names(self) -> list:
        return [n for n, s in zip(self.settlements.names, self.gate_status())
                if s == STATUS_CUT_OFF]

    def report(self) -> dict:
        from collections import Counter

        counts = {}
        for key, values in sorted(self.status.items(), key=lambda kv: str(kv[0])):
            kind, target, scenario = key
            counts.setdefault(kind, {}).setdefault(target, {})[scenario] = dict(
                sorted(Counter(values).items()))
        return {
            "definition": {
                "cut_off": ("no route on the graph to the nearest destination after the "
                            "flood, where a route existed before it"),
                "detour": (f"a post-flood route longer than "
                           f"{self.params.get('network', {}).get('detour_factor')}x the "
                           f"pre-flood route"),
                "no_route_before_the_flood": ("no mapped road reaches this settlement, or "
                                              "none reaches a destination even with no "
                                              "flood: NOT a flood impact"),
                "gated_definition": (f"graph {CUTOFF_GATE_GRAPH}, destination "
                                     f"{CUTOFF_GATE_TARGET}, scenario "
                                     f"{CUTOFF_GATE_SCENARIO}"),
            },
            "parameters": self.params,
            "settlements": self.settlements.report(),
            "destinations": self.targets_found,
            "graphs": {k: {"nodes": g.n_nodes, "edges": g.n_edges,
                           "length_km": round(g.total_length_m() / 1000.0, 2)}
                       for k, g in sorted(self.graphs.items())},
            "status_counts": counts,
            "cut_off_by_the_gated_definition": len(self.cut_off_names()),
            "probability": {
                "runs": self.n_runs,
                "what_it_is": ("share of a perturbed ensemble in which the settlement is cut "
                               "off by the gated definition. A SENSITIVITY measure, not a "
                               "calibrated probability: nothing here is validated against "
                               "observed cut-off outcomes"),
                "mean": (round(float(np.mean(self.probability)), 4)
                         if len(self.probability) else None),
                "at_or_above_0.5": int(np.sum(self.probability >= 0.5)),
                **self.ensemble,
            },
            "caveat": CUTOFF_CAVEAT,
        }


def assess(classes: np.ndarray, z_combined: np.ndarray, reliability: np.ndarray | None,
           grid, osm_layers: dict, z_seed: float,
           np_: NetworkParams | None = None, mc: MonteCarloParams | None = None,
           dmgp: DamageParams | None = None, extent: ExtentParams | None = None,
           bridges_out_ids: set | None = None, log=print) -> CutoffResult:
    """The whole cut-off analysis. Refuses rather than inventing an answer."""
    from strandline import damage, osm as osm_mod

    np_ = np_ if np_ is not None else NetworkParams()
    mc = mc if mc is not None else MonteCarloParams()
    dmgp = dmgp if dmgp is not None else DamageParams()
    crs = grid.crs

    roads = osm_mod.to_geodataframe(osm_layers.get("roads") or {}, crs)
    if len(roads) == 0:
        raise NetworkError(
            "the OpenStreetMap snapshot for this area has NO roads at all, so no route can "
            "be computed and no settlement can be called cut off. Report the cut-off "
            "analysis as NOT AVAILABLE for this area; the flood and damage maps are "
            "unaffected.")
    segments = damage.split_at_junctions(roads, dmgp.min_segment_m)

    places = osm_mod.to_geodataframe(osm_layers.get("places") or {}, crs)
    buildings = osm_mod.to_geodataframe(osm_layers.get("buildings") or {}, crs)
    settlements = find_settlements(places, buildings, np_, log=log)
    if len(settlements) == 0:
        raise NetworkError(
            "the OpenStreetMap snapshot for this area has NO settlements: no place node of "
            f"type {SETTLEMENT_PLACE_TYPES} and no cluster of at least "
            f"{np_.cluster_min_buildings} buildings within {np_.cluster_eps_m:g} m. There is "
            "nobody to be cut off in this data. Report the cut-off analysis as NOT "
            "AVAILABLE; this is an OSM completeness limit, not a finding that nobody is "
            "isolated.")

    health = osm_mod.to_geodataframe(osm_layers.get("health") or {}, crs)
    health_xy = (np.column_stack([health.geometry.centroid.x.to_numpy(),
                                  health.geometry.centroid.y.to_numpy()])
                 if len(health) else np.zeros((0, 2)))
    town_xy = np.array([settlements.xy[i] for i, k in enumerate(settlements.kinds)
                        if k in TOWN_PLACE_TYPES]).reshape(-1, 2)

    graphs = {k: build_graph(segments, grid, k, np_) for k in GRAPH_KINDS}
    attached = {k: attach(settlements.xy, g, np_.settlement_link_max_m)
                for k, g in graphs.items()}
    settlements.not_attached = int(np.sum(
        np.all(np.column_stack([attached[k] for k in sorted(attached)]) < 0, axis=1))
        if len(settlements) else 0)
    target_nodes = {
        (k, "health"): attach(health_xy, g, np_.settlement_link_max_m)
        for k, g in graphs.items()}
    target_nodes.update({
        (k, "town"): attach(town_xy, g, np_.settlement_link_max_m)
        for k, g in graphs.items()})
    for key in list(target_nodes):
        target_nodes[key] = target_nodes[key][target_nodes[key] >= 0]

    # Which edges disappear in the bridges-failed scenario. Given the set of
    # bridges the damage overlay flagged "likely out", only those fail - the
    # what-if is "the crossings we have reason to doubt are gone", not "every
    # bridge in the country collapses". With no such set, every bridge edge
    # fails, which is the cruder reading and is recorded as such.
    bridge_out_edge = {}
    for k, g in graphs.items():
        if bridges_out_ids is None:
            bridge_out_edge[k] = g.edge_is_bridge.copy()
        else:
            parents = [str(sid).split("#")[0] for sid in g.edge_segment_id]
            bridge_out_edge[k] = np.array([p in bridges_out_ids for p in parents],
                                          dtype=bool)

    base_mask = flood_flat_mask(classes, grid, extent, dmgp.buffer_px)
    log("      graphs: " + ", ".join(
        f"{k} {g.n_edges} edge(s), {g.total_length_m() / 1000.0:.1f} km"
        for k, g in sorted(graphs.items())))

    before_m, after_m, status = {}, {}, {}
    for kind, g in graphs.items():
        flooded = g.flooded_edges(base_mask)
        none_removed = np.zeros(g.n_edges, dtype=bool)
        for target in TARGETS:
            sources = target_nodes[(kind, target)]
            before = distance_to_nearest(g, sources, none_removed)
            before_m[(kind, target)] = before
            for scenario in SCENARIOS:
                removed = _removed(g, flooded, scenario, bridge_out_edge[kind])
                after = distance_to_nearest(g, sources, removed)
                after_m[(kind, target, scenario)] = after
                status[(kind, target, scenario)] = statuses(
                    before, after, attached[kind], np_.detour_factor)

    probability, ensemble = monte_carlo(
        classes, z_combined, reliability, grid, graphs, attached, target_nodes,
        bridge_out_edge, z_seed, np_, mc, extent, log=log)

    result = CutoffResult(
        settlements=settlements, graphs=graphs, attached=attached, status=status,
        before_m=before_m, after_m=after_m, probability=probability, n_runs=mc.n_runs,
        target_nodes=target_nodes,
        params={"network": asdict(np_), "monte_carlo": asdict(mc),
                "damage": asdict(dmgp), "z_seed": z_seed},
        targets_found={"health_facilities": int(len(health_xy)),
                       "towns": int(len(town_xy)),
                       "health_reachable_on_vehicle_graph":
                           int(len(target_nodes[(GRAPH_VEHICLE, "health")])),
                       "health_reachable_on_foot_graph":
                           int(len(target_nodes[(GRAPH_FOOT, "health")]))},
        ensemble=ensemble,
    )
    counts = result.report()["status_counts"]
    log(f"      cut off by the gated definition: {len(result.cut_off_names())} of "
        f"{len(settlements)} settlement(s); vehicle/health/intact {counts.get(GRAPH_VEHICLE, {}).get('health', {}).get(SCENARIO_BRIDGES_INTACT, {})}")
    return result


# --- the ensemble --------------------------------------------------------------

def monte_carlo(classes, z_combined, reliability, grid, graphs, attached, target_nodes,
                bridge_out_edge, z_seed: float, np_: NetworkParams,
                mc: MonteCarloParams, extent: ExtentParams | None = None,
                log=print) -> tuple[np.ndarray, dict]:
    """Share of perturbed runs in which each settlement is cut off.

    One `numpy.random.default_rng(seed)` drives every draw in a fixed order, so
    the same inputs give byte-identical output (Rule 5). Only the GATED
    definition is ensembled - a probability per graph, destination and scenario
    would be four numbers nobody asked for, and the gated one is the number the
    outputs quote.
    """
    kind, target, scenario = CUTOFF_GATE_GRAPH, CUTOFF_GATE_TARGET, CUTOFF_GATE_SCENARIO
    graph = graphs[kind]
    nodes = attached[kind]
    sources = target_nodes[(kind, target)]
    n_settlements = len(nodes)
    if n_settlements == 0:
        return np.zeros(0), {"note": "no settlements to ensemble"}

    rng = np.random.default_rng(mc.seed)
    narrow = (reliability == RELIABILITY_NARROW_VALLEY) if reliability is not None else None
    n_narrow = int(narrow.sum()) if narrow is not None else 0
    lo, hi = (float(v) for v in mc.z_seed_delta)
    choices = [int(b) for b in mc.buffer_px_choices]

    # The pre-flood route removes no edges, so it is the same in every member
    # of the ensemble: computed once, outside the loop.
    before = distance_to_nearest(graph, sources, np.zeros(graph.n_edges, dtype=bool))
    cut = np.zeros(n_settlements, dtype=np.int64)
    usable = 0
    deltas, buffers = [], []
    for _ in range(mc.n_runs):
        delta = float(rng.uniform(lo, hi))
        buffer_px = int(choices[int(rng.integers(len(choices)))])
        flips = None
        if n_narrow:
            flips = np.zeros(narrow.shape, dtype=bool)
            flips[narrow] = rng.random(n_narrow) < mc.narrow_valley_flood_probability
        mask = perturbed_flat_mask(classes, z_combined, reliability, z_seed, delta,
                                   buffer_px, flips, extent)
        flooded = graph.flooded_edges(mask)
        removed = _removed(graph, flooded, scenario, bridge_out_edge[kind])
        after = distance_to_nearest(graph, sources, removed)
        run_status = statuses(before, after, nodes, np_.detour_factor)
        cut += np.array([s == STATUS_CUT_OFF for s in run_status], dtype=np.int64)
        deltas.append(delta)
        buffers.append(buffer_px)
        usable += 1
    log(f"      ensemble: {usable} run(s), seed {mc.seed}, "
        f"{n_narrow} low-confidence pixel(s) flipped per run")
    return cut / max(usable, 1), {
        "seed": mc.seed,
        "z_seed_used": z_seed,
        "z_seed_delta_range": [lo, hi],
        "z_seed_delta_mean_drawn": round(float(np.mean(deltas)), 4) if deltas else None,
        "buffer_px_choices": choices,
        "buffer_px_mean_drawn": round(float(np.mean(buffers)), 4) if buffers else None,
        "narrow_valley_pixels": n_narrow,
        "narrow_valley_flood_probability": mc.narrow_valley_flood_probability,
    }


# --- outputs -------------------------------------------------------------------

def write_layers(out: Path, result: CutoffResult, grid, log=print) -> dict[str, int]:
    """settlements.geojson and routes.geojson, written even when empty."""
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    s = result.settlements
    rows = []
    for i in range(len(s)):
        props = {
            "name": s.names[i],
            "kind": s.kinds[i],
            "source": s.sources[i],
            "status": result.gate_status()[i],
            "cut_off_probability": round(float(result.probability[i]), 4),
            "probability_is": ("share of a perturbed ensemble, a sensitivity measure, "
                               "NOT a calibrated chance"),
        }
        if s.n_buildings[i] is not None:
            props["n_buildings"] = s.n_buildings[i]
        for kind in sorted(result.graphs):
            for target in TARGETS:
                before = result.before_m[(kind, target)]
                node = result.attached[kind][i]
                b = (float(before[node]) if node >= 0 and np.isfinite(before[node])
                     else None)
                props[f"{kind}_{target}_before_m"] = (round(b, 1) if b is not None else None)
                for scenario in SCENARIOS:
                    after = result.after_m[(kind, target, scenario)]
                    a = (float(after[node]) if node >= 0 and np.isfinite(after[node])
                         else None)
                    props[f"{kind}_{target}_{scenario}_after_m"] = (
                        round(a, 1) if a is not None else None)
                    props[f"{kind}_{target}_{scenario}_status"] = \
                        result.status[(kind, target, scenario)][i]
        rows.append(props)

    geoms = [Point(xy) for xy in s.xy] if len(s) else []
    settle = gpd.GeoDataFrame(rows, geometry=geoms, crs=grid.crs) if rows else None
    _write(out / "settlements.geojson", settle, "settlements", result, log)

    # routes.geojson: the straight link from each settlement to the graph node it
    # was routed from, per graph. Deliberately NOT a drawn shortest path - the
    # router returns distances, not paths, and inventing a polyline that was
    # never computed would be a picture of something we did not do.
    route_rows, route_geoms = [], []
    for kind, graph in sorted(result.graphs.items()):
        for i in range(len(s)):
            node = result.attached[kind][i]
            if node < 0:
                continue
            route_rows.append({
                "settlement": s.names[i], "graph": kind,
                "attached_node_distance_m": round(float(np.hypot(
                    *(graph.nodes[node] - s.xy[i]))), 1),
                "status": result.status[(kind, CUTOFF_GATE_TARGET,
                                         CUTOFF_GATE_SCENARIO)][i],
                "note": "link from the settlement to the road node it routes from, "
                        "not a drawn shortest path",
            })
            route_geoms.append(LineString([tuple(s.xy[i]), tuple(graph.nodes[node])]))
    routes = (gpd.GeoDataFrame(route_rows, geometry=route_geoms, crs=grid.crs)
              if route_rows else None)
    _write(out / "routes.geojson", routes, "routes", result, log)
    return {"settlements": len(rows), "routes": len(route_rows)}


def _write(path: Path, gdf, layer: str, result: CutoffResult, log=print) -> None:
    features = []
    if gdf is not None and len(gdf):
        wgs = gdf.to_crs("EPSG:4326")
        for (_, row), geom in zip(gdf.iterrows(), wgs.geometry):
            props = {k: (v.item() if hasattr(v, "item") else v)
                     for k, v in row.drop(labels="geometry", errors="ignore").items()}
            features.append({"type": "Feature", "properties": props,
                             "geometry": json.loads(json.dumps(geom.__geo_interface__))})
    body = {"type": "FeatureCollection",
            "properties": {"layer": layer, "caveat": CUTOFF_CAVEAT,
                           "gated_definition": {
                               "graph": CUTOFF_GATE_GRAPH,
                               "destination": CUTOFF_GATE_TARGET,
                               "scenario": CUTOFF_GATE_SCENARIO},
                           "ensemble_runs": result.n_runs,
                           "attribution": "© OpenStreetMap contributors"},
            "features": features}
    path.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
    log(f"      wrote {path.name} ({len(features)})")
