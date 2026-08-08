"use client";

import { useState } from "react";
import { useRailwayTelemetry } from "@/hooks/useRailwayTelemetry";
import PanelHeader from "./PanelHeader";
import SectionPanel from "./SectionPanel";
import TrainRoster from "./TrainRoster";
import ConflictDeck from "./ConflictDeck";

/**
 * The only component that owns the socket. Everything below it receives the
 * store and subscribes to the channel it needs.
 *
 * `focusedTrainId` is the one piece of genuinely UI-local state here -- it
 * crosses from the roster (React) to the panel (canvas) via a ref inside
 * SectionPanel, so hovering a row does not restart the frame loop.
 */
export default function ControllerDashboard() {
  const { store, connection, dispatchAction } = useRailwayTelemetry();
  const [focusedTrainId, setFocusedTrainId] = useState<string | null>(null);

  return (
    <div className="flex h-dvh flex-col bg-[var(--panel-bg)] text-[var(--panel-text)]">
      <PanelHeader store={store} connection={connection} />

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* Left: the panel over the roster. On narrow screens they stack. */}
        <div className="grid min-h-0 grid-rows-[minmax(220px,1fr)_minmax(180px,320px)]">
          <section
            aria-label="Track diagram"
            className="min-h-0 border-b border-[var(--panel-line)]"
          >
            <SectionPanel store={store} focusedTrainId={focusedTrainId} />
          </section>

          <section aria-label="Section roster" className="min-h-0 bg-[var(--panel-bg)]">
            <TrainRoster
              store={store}
              focusedTrainId={focusedTrainId}
              onFocus={setFocusedTrainId}
            />
          </section>
        </div>

        {/* Right: decisions. Kept in its own column so it never scrolls away. */}
        <aside
          aria-label="Conflict advisories"
          className="min-h-0 border-t border-[var(--panel-line)] bg-[var(--panel-bg)] lg:border-l lg:border-t-0"
        >
          <h2 className="border-b border-[var(--panel-line)] bg-[var(--panel-raised)] px-3 py-2.5 font-mono text-[9px] font-bold uppercase tracking-[0.24em] text-[var(--panel-muted)]">
            Advisories
          </h2>
          <div className="h-[calc(100%-37px)]">
            <ConflictDeck store={store} onCommit={dispatchAction} />
          </div>
        </aside>
      </div>
    </div>
  );
}
