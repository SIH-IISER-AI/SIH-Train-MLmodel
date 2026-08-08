"use client";

import { delayMinutes, speedRatio } from "@/lib/contracts";
import { TelemetryStore } from "@/lib/telemetryStore";
import { useTrains } from "@/hooks/useRailwayTelemetry";

/**
 * The tabular read of the same Map the panel paints from.
 *
 * Subscribes to the store's "trains" channel, which flushes at 10Hz -- fast
 * enough that a controller never sees stale numbers, slow enough that a burst
 * of 40 telemetry packets produces one render instead of forty.
 *
 * Sort order is store.roster(): conflicted first, then by delay, then by
 * priority weight. That is triage order, not train-number order.
 */

interface Props {
  store: TelemetryStore;
  focusedTrainId: string | null;
  onFocus: (trainId: string | null) => void;
}

const ASPECT_CLASS: Record<string, string> = {
  RED: "bg-[var(--aspect-red)]",
  YELLOW: "bg-[var(--aspect-yellow)]",
  DOUBLE_YELLOW: "bg-[var(--aspect-yellow)]",
  GREEN: "bg-[var(--aspect-green)]",
};

export default function TrainRoster({ store, focusedTrainId, onFocus }: Props) {
  const trains = useTrains(store);

  if (trains.length === 0) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <p className="font-mono text-[11px] leading-5 text-[var(--panel-muted)]">
          Roster is empty.
          <br />
          It fills as trains report on section.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <table className="w-full border-collapse text-left">
        <thead className="sticky top-0 z-10 bg-[var(--panel-raised)]">
          <tr className="border-b border-[var(--panel-line)]">
            {["Train", "Block", "Speed", "Delay", "Sig"].map((label) => (
              <th
                key={label}
                scope="col"
                className="px-3 py-2 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-[var(--panel-muted)]"
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trains.map((train) => {
            const t = train.telemetry;
            const late = delayMinutes(t);
            const conflicted = store.isInConflict(t.train_id);
            const focused = focusedTrainId === t.train_id;

            return (
              <tr
                key={t.train_id}
                onMouseEnter={() => onFocus(t.train_id)}
                onMouseLeave={() => onFocus(null)}
                onFocus={() => onFocus(t.train_id)}
                onBlur={() => onFocus(null)}
                tabIndex={0}
                className={[
                  "border-b border-[var(--panel-line)] outline-none transition-colors",
                  "focus-visible:bg-[var(--panel-hover)] focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--panel-text)]",
                  focused ? "bg-[var(--panel-hover)]" : "",
                  conflicted ? "bg-[var(--aspect-red)]/[0.08]" : "",
                ].join(" ")}
              >
                <td className="px-3 py-2">
                  <div className="flex items-baseline gap-2">
                    <span className="font-mono text-[13px] font-semibold tabular-nums text-[var(--panel-text)]">
                      {t.train_id}
                    </span>
                    {conflicted && (
                      <span className="font-mono text-[9px] font-bold uppercase tracking-widest text-[var(--aspect-red)]">
                        Conflict
                      </span>
                    )}
                  </div>
                  <div className="truncate font-sans text-[11px] text-[var(--panel-muted)]">
                    {t.train_name}
                    <span className="mx-1.5 text-[var(--panel-line)]">/</span>
                    <span className="tabular-nums">P{t.priority_weight.toFixed(1)}</span>
                  </div>
                </td>

                <td className="px-3 py-2 font-mono text-[11px] tabular-nums text-[var(--panel-muted)]">
                  {t.current_block_id}
                  <div className="text-[10px] text-[var(--panel-line)]">→ {t.next_station_id}</div>
                </td>

                <td className="px-3 py-2">
                  <div className="font-mono text-[12px] tabular-nums text-[var(--panel-text)]">
                    {Math.round(t.speed_kmh)}
                  </div>
                  {/* Speed against the permitted ceiling -- the number a
                      controller actually cares about is the ratio, not the raw. */}
                  <div className="mt-1 h-[3px] w-12 bg-[var(--panel-line)]">
                    <div
                      className="h-full bg-[var(--panel-rail-live)]"
                      style={{ width: `${speedRatio(t) * 100}%` }}
                    />
                  </div>
                </td>

                <td className="px-3 py-2">
                  <span
                    className={[
                      "font-mono text-[12px] font-semibold tabular-nums",
                      late >= 15
                        ? "text-[var(--aspect-red)]"
                        : late >= 5
                          ? "text-[var(--aspect-yellow)]"
                          : "text-[var(--panel-muted)]",
                    ].join(" ")}
                  >
                    {late > 0 ? `+${late}` : late < 0 ? late : "—"}
                  </span>
                </td>

                <td className="px-3 py-2">
                  <span
                    className={`inline-block h-2.5 w-2.5 rounded-full ${ASPECT_CLASS[t.signal_aspect] ?? "bg-[var(--panel-line)]"}`}
                    title={t.signal_aspect}
                  />
                  <span className="sr-only">{t.signal_aspect}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
