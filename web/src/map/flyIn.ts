/* The one orchestrated moment: four seconds from high above the valley down
   into it, the water running downstream, the strandline drawing itself.

   One sequence, once per session, skipped entirely under reduced motion.
   See web/DESIGN.md section 3. */

import type { Map as MLMap } from "maplibre-gl";

import { FULL_REVEAL, setReveal, setStrandlineReveal } from "./layers";

export interface Viewpoint {
  center: [number, number];
  zoom: number;
  bearing: number;
  pitch: number;
}

export const prefersReducedMotion = (): boolean =>
  typeof window !== "undefined" &&
  !!window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const CAMERA_MS = 2400;
const WATER = [1200, 3000] as const;
const STRAND = [2600, 4000] as const;
const TOTAL = 4000;

const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3);
const easeInOutSine = (t: number) => -(Math.cos(Math.PI * t) - 1) / 2;

/** Map a progress fraction onto the reveal range the opacity expressions use. */
const toReveal = (p: number) => -0.12 + p * (FULL_REVEAL + 0.12);

export interface FlyIn {
  /** Jump to the end: full reveal, final camera, no animation left running. */
  finish(): void;
  /** True once the sequence has ended, by itself or by finish(). */
  done(): boolean;
}

/**
 * Start the sequence. Returns a handle whose `finish()` is what the skip
 * affordance and `prefers-reduced-motion` both call.
 */
export function startFlyIn(map: MLMap, to: Viewpoint, onEnd?: () => void): FlyIn {
  let frame = 0;
  let ended = false;

  const settle = () => {
    setReveal(map, FULL_REVEAL);
    setStrandlineReveal(map, FULL_REVEAL);
  };

  const end = () => {
    if (ended) return;
    ended = true;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    settle();
    onEnd?.();
  };

  if (prefersReducedMotion()) {
    map.jumpTo(to);
    end();
    return { finish: end, done: () => ended };
  }

  // Start high and flat, looking down the valley we are about to drop into.
  map.jumpTo({
    center: to.center,
    zoom: Math.max(2, to.zoom - 3.4),
    bearing: to.bearing,
    pitch: 0,
  });
  setReveal(map, -0.12);
  setStrandlineReveal(map, -0.12);

  map.easeTo({
    ...to,
    duration: CAMERA_MS,
    easing: easeOutCubic,
    essential: true,
  });

  const t0 = performance.now();
  const step = () => {
    if (ended) return;
    const elapsed = performance.now() - t0;

    const water = clamp01((elapsed - WATER[0]) / (WATER[1] - WATER[0]));
    setReveal(map, toReveal(easeInOutSine(water)));

    const strand = clamp01((elapsed - STRAND[0]) / (STRAND[1] - STRAND[0]));
    setStrandlineReveal(map, toReveal(easeOutCubic(strand)));

    if (elapsed >= TOTAL) {
      end();
      return;
    }
    frame = requestAnimationFrame(step);
  };
  frame = requestAnimationFrame(step);

  return {
    finish: () => {
      map.stop();
      map.jumpTo(to);
      end();
    },
    done: () => ended,
  };
}

/** The viewpoint the dashboard rests at: the whole area, pitched into 3D,
 *  looking downstream. */
export function restingViewpoint(
  map: MLMap,
  bbox: [number, number, number, number],
  bearing: number,
): Viewpoint {
  const camera = map.cameraForBounds(
    [
      [bbox[0], bbox[1]],
      [bbox[2], bbox[3]],
    ],
    { bearing, padding: { top: 40, bottom: 150, left: 260, right: 80 } },
  );
  const center = camera?.center as { lng: number; lat: number } | undefined;
  return {
    center: center ? [center.lng, center.lat] : [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2],
    // The 3D view eats some of the frame, so sit a touch wider than the fit.
    zoom: (camera?.zoom ?? 9.5) - 0.45,
    bearing,
    pitch: 60,
  };
}
