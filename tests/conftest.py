"""A synthetic Himalayan valley with KNOWN truth, for testing the detector.

Layout (240 x 240 px, 20 m): a river runs down the middle of a flat valley
floor, steep walls rise on both sides. Injected:
  - a flood on the valley floor beside the river (truth mask),
  - a debris fan that brightens radar (truth mask),
  - bait: a dark patch high on a mountain wall (radar shadow / wet snow),
    which must NOT be called flood,
  - a single dark speck that must be removed as noise.
"""

import numpy as np
import pytest

from strandline.terrain import slope_degrees

H = W = 240
RES = 20.0
CX = 120


def _geometry():
    rows, cols = np.mgrid[0:H, 0:W]
    d = np.abs(cols - CX)
    wall = np.where(d <= 20, 0.0, 7.0 * (d - 20))           # metres above valley floor
    dem = 700.0 - 0.5 * rows + wall
    river = d <= 3
    flood = (rows >= 60) & (rows <= 180) & (d > 3) & (d <= 17)
    debris = (rows >= 195) & (rows <= 220) & (d <= 15)
    bait = (rows >= 20) & (rows <= 40) & (cols >= 215) & (cols <= 235)
    speck = np.zeros((H, W), dtype=bool)
    speck[100, CX - 20] = True
    streams = d == 0
    return dict(rows=rows, d=d, dem=dem, hand=wall, river=river, flood=flood,
                debris=debris, bait=bait, speck=speck, streams=streams)


def _base_db(g, post: bool, with_event: bool):
    vv = np.full((H, W), -9.0)
    vh = np.full((H, W), -16.0)
    vv[g["river"]], vh[g["river"]] = -21.0, -28.0
    if post and with_event:
        vv[g["flood"]], vh[g["flood"]] = -19.0, -26.0
        vv[g["debris"]], vh[g["debris"]] = -4.0, -11.0
        vv[g["bait"]], vh[g["bait"]] = -20.0, -27.0
        vv[g["speck"]], vh[g["speck"]] = -20.0, -27.0
    return vv, vh


@pytest.fixture
def scene():
    """Returns a builder: scene(event=True, speckle=False, n_pre=5, seed=0)."""
    g = _geometry()
    slope = slope_degrees(g["dem"], RES)

    def build(event=True, speckle=False, n_pre=5, seed=0):
        rng = np.random.default_rng(seed)

        def observe(vv_db, vh_db):
            if speckle:  # linear intensity with gamma speckle (ENL 4.4)
                lin = [10 ** (b / 10) * rng.gamma(4.4, 1 / 4.4, size=b.shape) for b in (vv_db, vh_db)]
                return lin[0], lin[1]
            return vv_db + rng.normal(0, 1.0, vv_db.shape), vh_db + rng.normal(0, 1.0, vh_db.shape)

        pre = [observe(*_base_db(g, post=False, with_event=False)) for _ in range(n_pre)]
        post = observe(*_base_db(g, post=True, with_event=event))
        return dict(geometry=g, slope=slope, pre=pre, post=post, speckle=speckle)

    return build
