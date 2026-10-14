/* The intro: the valley as a sculpted model, lit like a studio render, with
   the flood as a glowing ribbon of meltwater along the river.

   The terrain is read from the run's own Copernicus DEM — the same terrarium
   tiles the map uses, decoded in a canvas — so nothing here is a drawing of a
   place: it is the elevation the pipeline worked on. One slow orbit, one line
   of text, one way in. Skipped under reduced motion and on low-power devices,
   and "Skip intro" is on screen the whole time.

   Budget: two 256px tiles, one mesh, no textures, no loaders. */

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

import { api } from "../lib/api";
import type { Viewpoint } from "../map/flyIn";
import { prefersReducedMotion } from "../map/flyIn";

interface Props {
  runId: string;
  bbox: [number, number, number, number] | null;
  onOpen(from: Viewpoint | null): void;
}

const GRID = 128;
const C = {
  rock: 0x4a5a68,
  deep: 0x1b2430,
  water: 0x6cc6c2,
};

/** Longitude/latitude to the web-mercator tile that contains it. */
function tileOf(lng: number, lat: number, z: number): [number, number] {
  const n = 2 ** z;
  const x = Math.floor(((lng + 180) / 360) * n);
  const latRad = (lat * Math.PI) / 180;
  const y = Math.floor(((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n);
  return [x, y];
}

async function loadHeights(runId: string, bbox: [number, number, number, number]) {
  const z = 10;
  const [x, y] = tileOf((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, z);
  const image = new Image();
  image.crossOrigin = "anonymous";
  const url = api.terrainTiles(runId)
    .replace("{z}", String(z))
    .replace("{x}", String(x))
    .replace("{y}", String(y));

  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("terrain tile did not load"));
    image.src = url;
  });

  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2d context");
  ctx.drawImage(image, 0, 0);
  const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);

  const heights = new Float32Array(GRID * GRID);
  let low = Infinity;
  let high = -Infinity;
  for (let row = 0; row < GRID; row += 1) {
    for (let col = 0; col < GRID; col += 1) {
      const sx = Math.min(width - 1, Math.round((col / (GRID - 1)) * (width - 1)));
      const sy = Math.min(height - 1, Math.round((row / (GRID - 1)) * (height - 1)));
      const i = (sy * width + sx) * 4;
      const metres = data[i] * 256 + data[i + 1] + data[i + 2] / 256 - 32768;
      heights[row * GRID + col] = metres;
      if (metres < low) low = metres;
      if (metres > high) high = metres;
    }
  }
  return { heights, low, high };
}

export function Intro({ runId, bbox, onOpen }: Props) {
  const mount = useRef<HTMLDivElement | null>(null);
  const viewpoint = useRef<Viewpoint | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "flat">("loading");

  useEffect(() => {
    if (!mount.current || !bbox) return;
    const host = mount.current;
    const reduced = prefersReducedMotion();

    const scene = new THREE.Scene();
    scene.fog = new THREE.Fog(C.deep, 2.2, 7.5);

    const camera = new THREE.PerspectiveCamera(38, host.clientWidth / host.clientHeight, 0.1, 100);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    renderer.setSize(host.clientWidth, host.clientHeight);
    renderer.setClearColor(C.deep, 1);
    host.appendChild(renderer.domElement);

    // Studio light: one key from the north-west, one cool fill, no sun disc.
    const key = new THREE.DirectionalLight(0xdfe9f0, 3.1);
    key.position.set(-2.4, 3.2, 1.8);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x7f9fba, 0.8);
    fill.position.set(2.6, 1.1, -2.2);
    scene.add(fill);
    scene.add(new THREE.AmbientLight(0x3b4c5c, 1.4));

    const geometry = new THREE.PlaneGeometry(3.2, 3.2, GRID - 1, GRID - 1);
    geometry.rotateX(-Math.PI / 2);
    const material = new THREE.MeshStandardMaterial({
      color: C.rock,
      roughness: 0.92,
      metalness: 0.02,
      flatShading: false,
    });
    const terrain = new THREE.Mesh(geometry, material);
    scene.add(terrain);

    // The meltwater ribbon is built once the heights are in; it is a tube, not
    // a GL line, so it has width and can glow.
    const ribbon = new THREE.Group();
    scene.add(ribbon);

    let frame = 0;
    let stopped = false;

    const draw = (t: number) => {
      const angle = reduced ? 0.6 : 0.6 + t * 0.00006;
      camera.position.set(Math.cos(angle) * 3.4, 2.05, Math.sin(angle) * 3.4);
      camera.lookAt(0, 0.15, 0);
      renderer.render(scene, camera);
    };

    const tick = (t: number) => {
      if (stopped) return;
      draw(t);
      frame = requestAnimationFrame(tick);
    };

    const onResize = () => {
      camera.aspect = host.clientWidth / host.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(host.clientWidth, host.clientHeight);
    };
    window.addEventListener("resize", onResize);

    loadHeights(runId, bbox)
      .then(({ heights, low, high }) => {
        if (stopped) return;
        const span = Math.max(1, high - low);
        const position = geometry.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < heights.length; i += 1) {
          // 0.9 of the model's width for 100% of its relief reads as a valley
          // without pretending to be a vertical scale.
          position.setY(i, ((heights[i] - low) / span) * 0.9);
        }
        position.needsUpdate = true;
        geometry.computeVertexNormals();

        // The lowest cell of each row across the model is the valley's
        // thalweg: where the water runs. Smoothed over five rows so the line
        // follows the valley rather than every pixel of noise.
        // Scan both ways and keep the one whose low line wanders least: that
        // is the one cutting across the valley, so its minima trace the river
        // rather than hopping between tributaries.
        // Outside the mapped area the DEM is a flat apron at its base height.
        // That apron is the lowest ground in the tile, so it has to be ruled
        // out or every minimum lands on the border instead of in the river.
        const apron = low + 0.5;
        const scan = (byRow: boolean) => {
          const line: number[] = [];
          for (let a = 0; a < GRID; a += 1) {
            let bestB = -1;
            let best = Infinity;
            for (let b = 0; b < GRID; b += 1) {
              const h = byRow ? heights[a * GRID + b] : heights[b * GRID + a];
              if (h <= apron) continue;
              if (h < best) {
                best = h;
                bestB = b;
              }
            }
            line.push(bestB);
          }
          let wander = 0;
          let previous = -1;
          for (const value of line) {
            if (value < 0) continue;
            if (previous >= 0) wander += Math.abs(value - previous);
            previous = value;
          }
          return { line, wander };
        };
        const byRow = scan(true);
        const byCol = scan(false);
        const chosen = byRow.wander <= byCol.wander ? byRow : byCol;
        const alongRows = chosen === byRow;

        const points: THREE.Vector3[] = [];
        for (let a = 2; a < GRID - 2; a += 2) {
          const window = [a - 2, a - 1, a, a + 1, a + 2]
            .map((i) => chosen.line[i])
            .filter((value) => value >= 0);
          if (window.length < 3 || chosen.line[a] < 0) continue;
          const b = Math.round(window.reduce((sum, value) => sum + value, 0) / window.length);
          const index = alongRows ? a * GRID + b : b * GRID + a;
          points.push(
            new THREE.Vector3(
              position.getX(index),
              position.getY(index) + 0.025,
              position.getZ(index),
            ),
          );
        }

        if (points.length > 3) {
          const curve = new THREE.CatmullRomCurve3(points);
          const core = new THREE.Mesh(
            new THREE.TubeGeometry(curve, 200, 0.012, 7, false),
            new THREE.MeshBasicMaterial({ color: C.water }),
          );
          const halo = new THREE.Mesh(
            new THREE.TubeGeometry(curve, 160, 0.045, 7, false),
            new THREE.MeshBasicMaterial({ color: C.water, transparent: true, opacity: 0.18 }),
          );
          ribbon.add(core, halo);
        }
        setState("ready");
      })
      .catch(() => {
        if (!stopped) setState("flat");
      });

    frame = requestAnimationFrame(tick);

    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", onResize);
      geometry.dispose();
      material.dispose();
      ribbon.children.forEach((child) => {
        const mesh = child as THREE.Mesh;
        mesh.geometry.dispose();
        (mesh.material as THREE.Material).dispose();
      });
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
  }, [runId, bbox]);

  return (
    <div className="intro">
      <div className="intro__scene" ref={mount} aria-hidden="true" />

      <button type="button" className="btn btn--quiet intro__skip" onClick={() => onOpen(null)}>
        Skip intro
      </button>

      <div className="intro__copy">
        <p className="intro__line">Where the flood reached, what it broke, who it stranded.</p>
        <button
          type="button"
          className="btn btn--primary intro__open"
          onClick={() => onOpen(viewpoint.current)}
        >
          Open the map
        </button>
        <p className="caption intro__note">
          {state === "flat"
            ? "Built from the run's Copernicus DEM, which has not loaded yet."
            : "A model of this run's own Copernicus DEM. Educational prototype."}
        </p>
      </div>
    </div>
  );
}
