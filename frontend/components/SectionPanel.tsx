"use client";

import { useEffect, useRef } from "react";
import { Coordinates } from "@/lib/contracts";
import { TelemetryStore, TrackedTrain } from "@/lib/telemetryStore";

/**
 * The illuminated track panel.
 *
 * This component renders ZERO React-managed state. It mounts once, grabs a
 * canvas, and runs a requestAnimationFrame loop that reads store.trains
 * directly on every frame. React never re-renders it, no matter how much
 * telemetry arrives.
 *
 * That is the whole reason the store keeps train state in a Map instead of
 * useState: 40 trains x one packet every 2.5s is 16 updates/sec, and each one
 * would otherwise reconcile a subtree. Here the socket mutates the Map, and the
 * frame loop paints whatever the Map currently says -- two loops running at
 * their own natural rates, meeting at a shared mutable object.
 *
 * Motion between packets comes from store.positionAt(), which extrapolates
 * along the last known heading at the last known speed. Without it, trains
 * would teleport 80m every 2.5s and the panel would read as a slideshow.
 */

interface Props {
  store: TelemetryStore;
  /** Train the roster is hovering, drawn with a highlight ring. */
  focusedTrainId?: string | null;
}

interface Bounds {
  minLat: number;
  maxLat: number;
  minLng: number;
  maxLng: number;
}

const PADDING = 56;
const BOUNDS_EASE = 0.04; // slow, so the view doesn't breathe as trains move

function readTheme(el: HTMLElement) {
  const style = getComputedStyle(el);
  const v = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback;
  return {
    rail: v("--panel-rail", "#3C4247"),
    railLive: v("--panel-rail-live", "#6E787F"),
    green: v("--aspect-green", "#3FD07A"),
    yellow: v("--aspect-yellow", "#F5B324"),
    red: v("--aspect-red", "#FF4438"),
    freight: v("--freight", "#8A9199"),
    text: v("--panel-text", "#D6DBE0"),
    muted: v("--panel-muted", "#7C858D"),
    grid: v("--panel-grid", "#22262A"),
  };
}

export default function SectionPanel({ store, focusedTrainId }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const boundsRef = useRef<Bounds | null>(null);
  const focusRef = useRef<string | null>(null);

  // Keep the focused id in a ref so changing it never restarts the frame loop.
  focusRef.current = focusedTrainId ?? null;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const theme = readTheme(wrap);
    let width = 0;
    let height = 0;

    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = wrap.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(wrap);

    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let raf = 0;
    let lastFrame = performance.now();

    const computeTargetBounds = (trains: TrackedTrain[]): Bounds | null => {
      if (trains.length === 0) return null;
      let minLat = Infinity;
      let maxLat = -Infinity;
      let minLng = Infinity;
      let maxLng = -Infinity;
      for (const train of trains) {
        for (const point of [train.rendered, ...train.trail]) {
          minLat = Math.min(minLat, point.lat);
          maxLat = Math.max(maxLat, point.lat);
          minLng = Math.min(minLng, point.lng);
          maxLng = Math.max(maxLng, point.lng);
        }
      }
      // Guard against a single train collapsing the box to a point.
      if (maxLat - minLat < 0.02) {
        const mid = (maxLat + minLat) / 2;
        minLat = mid - 0.01;
        maxLat = mid + 0.01;
      }
      if (maxLng - minLng < 0.02) {
        const mid = (maxLng + minLng) / 2;
        minLng = mid - 0.01;
        maxLng = mid + 0.01;
      }
      return { minLat, maxLat, minLng, maxLng };
    };

    const makeProjector = (b: Bounds) => {
      // Equirectangular with a cos(lat) correction so the corridor is not
      // stretched east-west. Fine at section scale; a section is ~200km.
      const latMid = (b.minLat + b.maxLat) / 2;
      const lngScale = Math.cos((latMid * Math.PI) / 180);
      const spanX = (b.maxLng - b.minLng) * lngScale;
      const spanY = b.maxLat - b.minLat;
      const usableW = Math.max(1, width - PADDING * 2);
      const usableH = Math.max(1, height - PADDING * 2);
      const scale = Math.min(usableW / spanX, usableH / spanY);
      const offsetX = (width - spanX * scale) / 2;
      const offsetY = (height - spanY * scale) / 2;

      return (c: Coordinates) => ({
        x: offsetX + (c.lng - b.minLng) * lngScale * scale,
        // Latitude increases north, canvas y increases down.
        y: offsetY + (b.maxLat - c.lat) * scale,
      });
    };

    const colorFor = (train: TrackedTrain) => {
      const { signal_aspect, train_type } = train.telemetry;
      if (train_type === "FREIGHT") return theme.freight;
      if (signal_aspect === "RED") return theme.red;
      if (signal_aspect === "YELLOW" || signal_aspect === "DOUBLE_YELLOW") return theme.yellow;
      return theme.green;
    };

    const drawGrid = () => {
      ctx.strokeStyle = theme.grid;
      ctx.lineWidth = 1;
      const step = 64;
      ctx.beginPath();
      for (let x = 0; x < width; x += step) {
        ctx.moveTo(Math.floor(x) + 0.5, 0);
        ctx.lineTo(Math.floor(x) + 0.5, height);
      }
      for (let y = 0; y < height; y += step) {
        ctx.moveTo(0, Math.floor(y) + 0.5);
        ctx.lineTo(width, Math.floor(y) + 0.5);
      }
      ctx.stroke();
    };

    const draw = (now: number) => {
      const frameDelta = Math.min(100, now - lastFrame);
      lastFrame = now;

      const trains = [...store.trains.values()];
      ctx.clearRect(0, 0, width, height);
      drawGrid();

      if (trains.length === 0) {
        ctx.fillStyle = theme.muted;
        ctx.font = "500 12px var(--font-geist-mono), monospace";
        ctx.textAlign = "center";
        ctx.fillText("NO TELEMETRY ON SECTION", width / 2, height / 2);
        ctx.font = "400 11px var(--font-geist-mono), monospace";
        ctx.fillText(
          "Start the simulator, or run with NEXT_PUBLIC_TELEMETRY_MODE=mock",
          width / 2,
          height / 2 + 18,
        );
        raf = requestAnimationFrame(draw);
        return;
      }

      // Advance every train's smoothed position before we need any of them.
      for (const train of trains) store.positionAt(train, now, reduceMotion ? 1000 : frameDelta);

      const target = computeTargetBounds(trains);
      if (target) {
        const current = boundsRef.current;
        boundsRef.current = current
          ? {
              minLat: current.minLat + (target.minLat - current.minLat) * BOUNDS_EASE,
              maxLat: current.maxLat + (target.maxLat - current.maxLat) * BOUNDS_EASE,
              minLng: current.minLng + (target.minLng - current.minLng) * BOUNDS_EASE,
              maxLng: current.maxLng + (target.maxLng - current.maxLng) * BOUNDS_EASE,
            }
          : target;
      }
      if (!boundsRef.current) {
        raf = requestAnimationFrame(draw);
        return;
      }

      const projectPoint = makeProjector(boundsRef.current);
      const pulse = reduceMotion ? 0.5 : (Math.sin(now / 320) + 1) / 2;

      // 1. Track trace. No topology contract exists yet, so the geometry of the
      //    section is inferred from where trains have actually been.
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      for (const train of trains) {
        if (train.trail.length < 2) continue;
        ctx.strokeStyle = theme.rail;
        ctx.lineWidth = 5;
        ctx.beginPath();
        train.trail.forEach((point, i) => {
          const p = projectPoint(point);
          if (i === 0) ctx.moveTo(p.x, p.y);
          else ctx.lineTo(p.x, p.y);
        });
        ctx.stroke();
      }

      // 2. Conflict tethers, drawn under the trains so markers stay readable.
      for (const conflict of store.openConflicts()) {
        const involved = conflict.conflicting_train_ids
          .map((id) => store.trains.get(id))
          .filter((t): t is TrackedTrain => Boolean(t));
        if (involved.length < 2) continue;

        const a = projectPoint(involved[0].rendered);
        const b = projectPoint(involved[1].rendered);
        ctx.save();
        ctx.strokeStyle = theme.red;
        ctx.globalAlpha = 0.35 + pulse * 0.45;
        ctx.lineWidth = 2;
        ctx.setLineDash([6, 6]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
        ctx.restore();

        const midX = (a.x + b.x) / 2;
        const midY = (a.y + b.y) / 2;
        const label = `T-${Math.max(0, Math.round(conflict.predicted_time_to_conflict_seconds / 60))} MIN`;
        ctx.font = "600 10px var(--font-geist-mono), monospace";
        const textWidth = ctx.measureText(label).width;
        ctx.fillStyle = theme.red;
        ctx.globalAlpha = 0.9;
        ctx.fillRect(midX - textWidth / 2 - 6, midY - 9, textWidth + 12, 18);
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#0F1113";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, midX, midY);
      }

      // 3. Trains, as heading-aligned chevrons.
      for (const train of trains) {
        const p = projectPoint(train.rendered);
        const color = colorFor(train);
        const inConflict = store.isInConflict(train.telemetry.train_id);
        const isFocused = focusRef.current === train.telemetry.train_id;
        const heading = ((train.fix.headingDeg - 90) * Math.PI) / 180;
        const size = train.telemetry.train_type === "FREIGHT" ? 6 : 8;

        if (inConflict || isFocused) {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 14 + (inConflict ? pulse * 6 : 3), 0, Math.PI * 2);
          ctx.strokeStyle = inConflict ? theme.red : theme.text;
          ctx.globalAlpha = inConflict ? 0.4 + pulse * 0.4 : 0.6;
          ctx.lineWidth = 2;
          ctx.stroke();
          ctx.globalAlpha = 1;
        }

        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(heading);
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(size * 1.4, 0);
        ctx.lineTo(-size, size * 0.8);
        ctx.lineTo(-size * 0.45, 0);
        ctx.lineTo(-size, -size * 0.8);
        ctx.closePath();
        ctx.fill();
        ctx.restore();

        // Label block: number over speed. Mono, because a controller reads
        // these as codes, not words.
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.font = "600 11px var(--font-geist-mono), monospace";
        ctx.fillStyle = isFocused || inConflict ? theme.text : theme.muted;
        ctx.fillText(train.telemetry.train_id, p.x + 14, p.y - 6);
        ctx.font = "400 10px var(--font-geist-mono), monospace";
        ctx.fillStyle = theme.muted;
        ctx.fillText(
          `${Math.round(train.telemetry.speed_kmh)} km/h`,
          p.x + 14,
          p.y + 7,
        );
      }

      raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [store]);

  return (
    <div ref={wrapRef} className="relative h-full w-full overflow-hidden">
      <canvas ref={canvasRef} className="block h-full w-full" aria-hidden="true" />
      <p className="sr-only">
        Live track diagram. Train positions are also listed in the section roster table.
      </p>
    </div>
  );
}
