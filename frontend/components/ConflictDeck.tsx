"use client";

import { useEffect, useRef, useState } from "react";
import { formatCountdown } from "@/lib/contracts";
import { TelemetryStore } from "@/lib/telemetryStore";
import { useConflicts } from "@/hooks/useRailwayTelemetry";

interface Props {
  store: TelemetryStore;
  onCommit: (conflictId: string, scenarioId: string) => boolean;
}

export default function ConflictDeck({ store, onCommit }: Props) {
  const { conflicts, acknowledged, recommendations } = useConflicts(store);
  const [, setTick] = useState(0);

const firstSeen = useRef<Map<string, number>>(new Map());

  // predicted_time_to_conflict_seconds is a snapshot taken when the engine
  // published, and the alert carries no timestamp -- so the countdown needs a
  // local anchor. A new prediction for the same conflict re-anchors.
  const seenAt = useRef(new Map<string, { seconds: number; at: number }>());

  const anchorFor = (conflictId: string, seconds: number) => {
    const prior = seenAt.current.get(conflictId);
    if (!prior || prior.seconds !== seconds) {
      const fresh = { seconds, at: Date.now() };
      seenAt.current.set(conflictId, fresh);
      return fresh;
    }
    return prior;
  };

  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 200);
    return () => clearInterval(id);
  }, []);

  if (conflicts.length === 0 && acknowledged.length === 0) {
    // A stopped train projects no future occupancy, so a queue standing at red
    // produces no detected overlap -- correct, and absurd to display as "clear".
    // No conflict means nothing to DECIDE, not that the section is running.
    const stopped = [...store.trains.values()].filter(
      (t) => t.telemetry.speed_kmh < 1,
    );
    const stalled = stopped.length > 0;

    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{
            backgroundColor: stalled ? "var(--aspect-yellow)" : "var(--aspect-green)",
          }}
        />
        <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-[var(--panel-muted)]">
          {stalled ? "No decision pending" : "Section clear"}
        </p>
        <p className="max-w-[30ch] font-sans text-[11px] leading-5 text-[var(--panel-muted)]">
          {stalled
            ? `${stopped.length} train${stopped.length > 1 ? "s" : ""} stationary — ` +
              `held or signal-checked. Nothing stopped projects a future overlap, ` +
              `so there is no precedence decision to make until the queue moves.`
            : "No spatial-temporal overlap predicted on the current horizon."}
        </p>
      </div>
    );
  }

  const inForceStrip = acknowledged.map((conflict) => {
    const plan = store.planFor(conflict.conflict_id);
    return (
      <article
        key={conflict.conflict_id}
        className="border border-[var(--panel-line)] bg-[var(--panel-raised)]/60 px-3 py-2"
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-mono text-[9px] font-bold uppercase tracking-[0.22em] text-[var(--aspect-green)]">
            Plan in force · {plan?.scenarioId ?? conflict.plan_in_force ?? "--"}
          </span>
          <span className="font-mono text-[9px] uppercase tracking-[0.18em] text-[var(--panel-muted)]">
            {conflict.conflict_id}
          </span>
        </div>
        <p className="mt-1 font-sans text-[11px] leading-[1.45] text-[var(--panel-muted)]">
          Still being re-solved every tick. The engine currently recommends the
          plan already executing, so there is nothing new to approve.
        </p>
      </article>
    );
  });

  return (
    <div className="h-full space-y-3 overflow-y-auto p-3">
      {inForceStrip}
      {conflicts.map((conflict) => {
        const recommendation = recommendations.get(conflict.conflict_id);
        const scenarios = [...(recommendation?.scenarios ?? [])].sort(
          (a, b) => (a.rank ?? 0) - (b.rank ?? 0),
        );

        const secondsLeft = store.secondsToConflict(conflict);

        const relaxed = scenarios.some((s) => s.policy_exceeded);
        const pending = store.pendingActions.get(conflict.conflict_id);
        const committed = store.planFor(conflict.conflict_id);
        const diverged = conflict.plan_state === "DIVERGED";
        const feedback =
          store.lastFeedback?.conflictId === conflict.conflict_id
            ? store.lastFeedback
            : null;

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
                    No precedence order resolves this conflict within the
                    standing limit on discretionary delay -- the wait a train
                    absorbs beyond what the occupied block physically forces.
                    The solver relaxed that limit to prevent a deadlock,
                    so every option below detains a train beyond standing
                    instructions. Requires supervisory authority.
                  </p>
                </div>
              </div>
            )}

            <div className="px-3 py-2.5">
              <p className="font-sans text-[12px] leading-5 text-[var(--panel-text)]">
                {conflict.root_cause}
              </p>
              <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--panel-muted)]">
                Section clearance time:{" "}
                <span className="text-[var(--aspect-yellow)]">
                  {conflict.estimated_cascading_impact_minutes} min
                </span>
              </p>
            </div>

            <div className="border-t border-[var(--panel-line)]">
              <h3 className="px-3 pt-2.5 font-mono text-[9px] font-semibold uppercase tracking-[0.22em] text-[var(--panel-muted)]">
                Dispatch options
              </h3>

              {diverged && (
                <p
                  role="alert"
                  className="mx-3 mt-2 border border-[var(--aspect-yellow)]/50 bg-[var(--aspect-yellow)]/[0.10] px-2.5 py-2 font-sans text-[11px] leading-[1.45] text-[var(--panel-text)]"
                >
                  <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[var(--aspect-yellow)]">
                    Plan superseded
                  </span>
                  <br />
                  The physical situation has moved since{" "}
                  {conflict.plan_in_force ?? "the accepted plan"} was approved.
                  Re-solving now returns a different precedence order. The
                  options below replace it.
                </p>
              )}

              {committed && !diverged && (
                <p
                  role="status"
                  className="px-3 pt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--aspect-green)]"
                >
                  {committed.scenarioId} already committed · re-approval is a no-op
                </p>
              )}

              {pending && (
                <p
                  role="status"
                  className="px-3 pt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--panel-muted)]"
                >
                  {pending.scenarioId} sent · awaiting simulator
                </p>
              )}

              {!pending && feedback?.outcome === "rejected" && (
                <p
                  role="alert"
                  className="px-3 pt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--aspect-red)]"
                >
                  {feedback.scenarioId} rejected · {feedback.reason}
                </p>
              )}

              {scenarios.length === 0 ? (
                (() => {
                  // "Solver running" is true for about a second. Past that,
                  // an empty scenario list is a RESULT -- every precedence
                  // order came back infeasible -- and saying "running" forever
                  // reads as a hang rather than an answer.
                  const seen = firstSeen.current;
                  if (!seen.has(conflict.conflict_id)) {
                    seen.set(conflict.conflict_id, Date.now());
                  }
                  const waited = Date.now() - (seen.get(conflict.conflict_id) ?? 0);
                  return (
                    <p className="px-3 pb-3 pt-2 font-mono text-[10px] leading-[1.5] text-[var(--panel-muted)]">
                      {waited < 8000
                        ? "Solver running. Options appear when the OR engine returns."
                        : "No feasible precedence order. Every ordering searched leaves a train committed to the resource with no loop to clear into — this conflict resolves by block-following, not by dispatch."}
                    </p>
                  );
                })()
              ) : (
                <ul className="space-y-px p-3 pt-2">
                  {scenarios.map((scenario) => {
                    const recommended = scenario.rank === 1;
                    const holds = (scenario.directives ?? []).filter(
                      (d) => d.kind === "HOLD_AT_LOOP",
                    ).length;
                    const alreadySent =
                      committed?.scenarioId === scenario.scenario_id && !diverged;

                    return (
                      <li key={scenario.scenario_id}>
                        <button
                          type="button"
                          disabled={pending !== undefined || alreadySent}
                          onClick={() => onCommit(conflict.conflict_id, scenario.scenario_id)}
                          className={[
                            "group w-full border px-3 py-2.5 text-left transition-colors",
                            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--panel-text)]",
                            "disabled:cursor-not-allowed disabled:opacity-50",
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
                              {alreadySent && (
                                <span className="ml-2 text-[var(--aspect-green)]">
                                  Committed
                                </span>
                              )}
                            </span>
                          </div>
                          <div className="mt-1 font-sans text-[12px] font-medium leading-5 text-[var(--panel-text)]">
                            {scenario.action}
                          </div>
                          <div className="mt-1 font-sans text-[11px] leading-5 text-[var(--panel-muted)]">
                            {scenario.network_impact}
                          </div>
                          {scenario.rationale && (
                            <div className="mt-1.5 font-sans text-[11px] leading-5 text-[var(--panel-muted)]">
                              {scenario.rationale}
                            </div>
                          )}
                          {holds > 1 && (
                            <div className="mt-1.5 font-mono text-[9px] uppercase tracking-[0.16em] text-[var(--panel-muted)]">
                              {holds} coordinated holds · applied together
                            </div>
                          )}
                          <div className="mt-2 h-[2px] w-full bg-[var(--panel-line)]">
                            <div
                              className={
                                scenario.policy_exceeded
                                  ? "h-full w-full bg-[var(--aspect-yellow)]"
                                  : recommended
                                    ? "h-full w-full bg-[var(--aspect-green)]"
                                    : "h-full w-full bg-[var(--panel-rail-live)]"
                              }
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