"use client";

import { useEffect, useState } from "react";
import { formatCountdown } from "@/lib/contracts";
import { TelemetryStore } from "@/lib/telemetryStore";
import { useConflicts } from "@/hooks/useRailwayTelemetry";

interface Props {
  store: TelemetryStore;
  onCommit: (conflictId: string, scenarioId: string) => boolean;
}

export default function ConflictDeck({ store, onCommit }: Props) {
  const { conflicts, recommendations } = useConflicts(store);
  const [now, setNow] = useState(() => Date.now());
  const [committing, setCommitting] = useState<string | null>(null);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  if (conflicts.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <span className="inline-block h-2 w-2 rounded-full bg-[var(--aspect-green)]" />
        <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-[var(--panel-muted)]">
          Section clear
        </p>
        <p className="max-w-[26ch] font-sans text-[11px] leading-5 text-[var(--panel-line)]">
          No spatial-temporal overlap predicted on the current horizon.
        </p>
      </div>
    );
  }

  return (
    <div className="h-full space-y-3 overflow-y-auto p-3">
      {conflicts.map((conflict) => {
        const recommendation = recommendations.get(conflict.conflict_id);
        const scenarios = [...(recommendation?.scenarios ?? [])].sort((a, b) => b.score - a.score);
        const secondsLeft = Math.max(
          0,
          conflict.predicted_time_to_conflict_seconds - Math.floor((now % 1000) / 1000),
        );
        const relaxed = scenarios.some((s) => s.policy_exceeded);

        return (
          <article
            key={conflict.conflict_id}
            className="border border-[var(--aspect-red)]/60 bg-[var(--panel-raised)]"
          >
            <header className="flex items-start justify-between gap-3 border-b border-[var(--panel-line)] bg-[var(--aspect-red)]/[0.12] px-3 py-2.5">
              <div>
                <div className="font-mono text-[9px] font-bold uppercase tracking-[0.22em] text-[var(--aspect-red)]">
                  {conflict.severity} · {conflict.conflict_id}
                </div>
                <div className="mt-1 font-mono text-[11px] text-[var(--panel-text)]">
                  {conflict.location.junction_id}
                  <span className="mx-1.5 text-[var(--panel-line)]">/</span>
                  {conflict.location.track_id}
                </div>
              </div>
              <div className="text-right">
                <div className="font-mono text-[22px] font-bold leading-none tabular-nums text-[var(--aspect-red)]">
                  {formatCountdown(secondsLeft)}
                </div>
                <div className="mt-1 font-mono text-[9px] uppercase tracking-[0.18em] text-[var(--panel-muted)]">
                  To conflict
                </div>
              </div>
            </header>

            {/* Every scenario for this conflict needed the hold cap relaxed. The
                controller is about to authorise something outside standing
                instructions and has to be told so BEFORE they click, not asked
                about it afterwards by an auditor. */}
            {relaxed && (
              <div
                role="alert"
                className="flex gap-2.5 border-b border-[var(--aspect-yellow)]/40 bg-[var(--aspect-yellow)]/[0.13] px-3 py-2.5"
              >
                <svg
                  viewBox="0 0 16 16"
                  aria-hidden="true"
                  className="mt-px h-4 w-4 shrink-0 fill-[var(--aspect-yellow)]"
                >
                  <path d="M8 1.2 15.2 14H.8L8 1.2Zm0 4.3a.85.85 0 0 0-.85.85v3.4a.85.85 0 1 0 1.7 0v-3.4A.85.85 0 0 0 8 5.5Zm0 6.1a.95.95 0 1 0 0 1.9.95.95 0 0 0 0-1.9Z" />
                </svg>
                <div>
                  <div className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--aspect-yellow)]">
                    Capacity exceeded · policy relaxation applied
                  </div>
                  <p className="mt-1 font-sans text-[11px] leading-[1.45] text-[var(--panel-text)]">
                    No precedence order resolves this conflict inside the
                    standard 45-minute maximum hold. The solver relaxed that
                    limit to prevent a deadlock, so every option below detains a
                    train beyond standing instructions. Requires supervisory
                    authority.
                  </p>
                </div>
              </div>
            )}

            <div className="px-3 py-2.5">
              <p className="font-sans text-[12px] leading-5 text-[var(--panel-text)]">
                {conflict.root_cause}
              </p>
              <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--panel-muted)]">
                Cascading impact if unresolved:{" "}
                <span className="text-[var(--aspect-yellow)]">
                  {conflict.estimated_cascading_impact_minutes} min
                </span>
              </p>
            </div>

            <div className="border-t border-[var(--panel-line)]">
              <h3 className="px-3 pt-2.5 font-mono text-[9px] font-semibold uppercase tracking-[0.22em] text-[var(--panel-muted)]">
                Dispatch options
              </h3>

              {scenarios.length === 0 ? (
                <p className="px-3 pb-3 pt-2 font-mono text-[10px] text-[var(--panel-line)]">
                  Solver running. Options appear when the OR engine returns.
                </p>
              ) : (
                <ul className="space-y-px p-3 pt-2">
                  {scenarios.map((scenario, index) => {
                    const recommended = index === 0;
                    const key = `${conflict.conflict_id}:${scenario.scenario_id}`;
                    const holds = (scenario.directives ?? []).filter(
                      (d) => d.kind === "HOLD_AT_LOOP",
                    ).length;

                    return (
                      <li key={scenario.scenario_id}>
                        <button
                          type="button"
                          disabled={committing !== null}
                          onClick={() => {
                            setCommitting(key);
                            const ok = onCommit(conflict.conflict_id, scenario.scenario_id);
                            if (!ok) setCommitting(null);
                          }}
                          className={[
                            "group w-full border px-3 py-2.5 text-left transition-colors",
                            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--panel-text)]",
                            "disabled:cursor-wait disabled:opacity-50",
                            scenario.policy_exceeded
                              ? "border-[var(--aspect-yellow)]/50 bg-[var(--aspect-yellow)]/[0.06] hover:bg-[var(--aspect-yellow)]/[0.12]"
                              : recommended
                                ? "border-[var(--aspect-green)]/50 bg-[var(--aspect-green)]/[0.07] hover:bg-[var(--aspect-green)]/[0.14]"
                                : "border-[var(--panel-line)] hover:bg-[var(--panel-hover)]",
                          ].join(" ")}
                        >
                          <div className="flex items-baseline justify-between gap-3">
                            <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--panel-muted)]">
                              {scenario.scenario_id}
                              {recommended && !scenario.policy_exceeded && (
                                <span className="ml-2 text-[var(--aspect-green)]">Best</span>
                              )}
                              {scenario.policy_exceeded && (
                                <span className="ml-2 text-[var(--aspect-yellow)]">
                                  Exceeds policy
                                </span>
                              )}
                            </span>
                            <span className="font-mono text-[12px] font-semibold tabular-nums text-[var(--panel-text)]">
                              {scenario.score.toFixed(2)}
                            </span>
                          </div>
                          <div className="mt-1 font-sans text-[12px] font-medium leading-5 text-[var(--panel-text)]">
                            {scenario.action}
                          </div>
                          <div className="mt-1 font-sans text-[11px] leading-5 text-[var(--panel-muted)]">
                            {scenario.network_impact}
                          </div>
                          {/* A grouped scenario is all-or-nothing at the
                              simulator. Showing the count makes that visible
                              rather than implying one button holds one train. */}
                          {holds > 1 && (
                            <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.16em] text-[var(--panel-line)]">
                              {holds} coordinated holds · applied together
                            </div>
                          )}
                          <div className="mt-2 h-[2px] w-full bg-[var(--panel-line)]">
                            <div
                              className={
                                scenario.policy_exceeded
                                  ? "h-full bg-[var(--aspect-yellow)]"
                                  : recommended
                                    ? "h-full bg-[var(--aspect-green)]"
                                    : "h-full bg-[var(--panel-rail-live)]"
                              }
                              style={{ width: `${Math.min(100, scenario.score * 100)}%` }}
                            />
                          </div>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}