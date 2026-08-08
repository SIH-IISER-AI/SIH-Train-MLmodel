"use client";

import { formatClock } from "@/lib/contracts";
import { TelemetryStore } from "@/lib/telemetryStore";
import { ConnectionState, useClock, useTrains } from "@/hooks/useRailwayTelemetry";

/**
 * Subscribes to "clock" (fires on SIMULATION_TICK, ~every 2s) for the tick and
 * health figures, and to "trains" for the counts. Two channels because the tick
 * and the roster move at different rates and neither should drag the other.
 */

interface Props {
  store: TelemetryStore;
  connection: ConnectionState;
}

const CONNECTION_COPY: Record<ConnectionState, { label: string; color: string }> = {
  connecting: { label: "Connecting", color: "var(--aspect-yellow)" },
  live: { label: "Live", color: "var(--aspect-green)" },
  reconnecting: { label: "Reconnecting", color: "var(--aspect-yellow)" },
  offline: { label: "Feed lost", color: "var(--aspect-red)" },
};

function Stat({
  label,
  value,
  unit,
  tone,
}: {
  label: string;
  value: string | number;
  unit?: string;
  tone?: string;
}) {
  return (
    <div className="px-4 py-2.5">
      <div className="font-mono text-[9px] font-semibold uppercase tracking-[0.2em] text-[var(--panel-muted)]">
        {label}
      </div>
      <div
        className="mt-1 font-mono text-[19px] font-semibold leading-none tabular-nums"
        style={{ color: tone ?? "var(--panel-text)" }}
      >
        {value}
        {unit && (
          <span className="ml-1 text-[11px] font-normal text-[var(--panel-muted)]">{unit}</span>
        )}
      </div>
    </div>
  );
}

export default function PanelHeader({ store, connection }: Props) {
  const clock = useClock(store);
  const trains = useTrains(store);

  const conflicts = store.openConflicts();
  const totalDelay = store.totalDelayMinutes();
  const health = clock?.network_health_score ?? 0;
  const status = CONNECTION_COPY[connection];

  return (
    <header className="flex flex-wrap items-stretch justify-between gap-y-2 border-b border-[var(--panel-line)] bg-[var(--panel-raised)]">
      <div className="flex items-center gap-4 px-4 py-2.5">
        <div>
          <h1 className="font-mono text-[13px] font-bold uppercase tracking-[0.24em] text-[var(--panel-text)]">
            NDLS–AGC 04
          </h1>
          <p className="mt-0.5 font-mono text-[9px] uppercase tracking-[0.16em] text-[var(--panel-muted)]">
            Section control · Digital co-pilot
          </p>
        </div>
      </div>

      <div className="flex flex-1 flex-wrap items-stretch justify-end divide-x divide-[var(--panel-line)]">
        <Stat label="Sim clock" value={formatClock(clock?.timestamp ?? 0)} />
        <Stat label="Rate" value={`${clock?.time_multiplier ?? 1}×`} />
        <Stat label="On section" value={trains.length} />
        <Stat
          label="Held delay"
          value={totalDelay}
          unit="min"
          tone={totalDelay > 60 ? "var(--aspect-red)" : undefined}
        />
        <Stat
          label="Conflicts"
          value={conflicts.length}
          tone={conflicts.length > 0 ? "var(--aspect-red)" : "var(--aspect-green)"}
        />
        <Stat
          label="Health"
          value={health.toFixed(1)}
          tone={
            health >= 80
              ? "var(--aspect-green)"
              : health >= 50
                ? "var(--aspect-yellow)"
                : "var(--aspect-red)"
          }
        />

        <div className="flex items-center gap-2 px-4 py-2.5">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{
              backgroundColor: status.color,
              boxShadow: connection === "live" ? `0 0 8px ${status.color}` : undefined,
            }}
          />
          <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-[var(--panel-muted)]">
            {status.label}
          </span>
        </div>
      </div>
    </header>
  );
}
