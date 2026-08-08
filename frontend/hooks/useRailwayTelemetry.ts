"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ControllerAction, isRailwayEvent } from "@/lib/contracts";
import { Channel, TelemetryStore, TrackedTrain } from "@/lib/telemetryStore";
import { startMockFeed } from "@/lib/mockFeed";

const SOCKET_URL = process.env.NEXT_PUBLIC_TELEMETRY_WS ?? "ws://localhost:8000/ws/telemetry";
const USE_MOCK = process.env.NEXT_PUBLIC_TELEMETRY_MODE === "mock";

const BACKOFF_MIN_MS = 500;
const BACKOFF_MAX_MS = 8_000;

export type ConnectionState = "connecting" | "live" | "reconnecting" | "offline";

/**
 * Owns exactly one socket and one store for the lifetime of the dashboard.
 *
 * Deliberately does NOT put train state in React state. `trainsRef.current` is
 * the same Map the store mutates; components that need re-renders get them via
 * the store's channel subscriptions (see useTrains / useClock / useConflicts).
 */
export function useRailwayTelemetry() {
  const storeRef = useRef<TelemetryStore | null>(null);
  if (storeRef.current === null) storeRef.current = new TelemetryStore();
  const store = storeRef.current;

  const socketRef = useRef<WebSocket | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  useEffect(() => {
    // Mock mode drives the same ingest path as the socket, so anything that
    // works here works against the real simulator. Set
    // NEXT_PUBLIC_TELEMETRY_MODE=mock to build UI without Redis running.
    if (USE_MOCK) {
      setConnection("live");
      const stop = startMockFeed((event) => store.ingest(event));
      return () => {
        stop();
        store.dispose();
      };
    }

    let disposed = false;
    let attempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (disposed) return;
      setConnection(attempt === 0 ? "connecting" : "reconnecting");

      const ws = new WebSocket(SOCKET_URL);
      socketRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        setConnection("live");
      };

      ws.onmessage = (event) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(event.data as string);
        } catch {
          store.droppedEvents += 1;
          console.warn("[telemetry] unparseable frame", event.data);
          return;
        }
        if (!isRailwayEvent(parsed)) {
          store.droppedEvents += 1;
          console.warn("[telemetry] unknown event_type -- contract drift?", parsed);
          return;
        }
        store.ingest(parsed);
      };

      ws.onerror = () => ws.close();

      ws.onclose = () => {
        socketRef.current = null;
        if (disposed) return;
        // Exponential backoff with jitter. Without this, one restart of the
        // ws-server leaves the panel dark until someone reloads the browser.
        const delay = Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * 2 ** attempt);
        attempt += 1;
        setConnection(attempt > 4 ? "offline" : "reconnecting");
        retryTimer = setTimeout(connect, delay + Math.random() * 250);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (retryTimer) clearTimeout(retryTimer);
      socketRef.current?.close();
      store.dispose();
    };
  }, [store]);

  /**
   * Contract 5, the only upstream message. Optimistically clears the conflict
   * card so the controller sees their decision land immediately; the simulator's
   * next telemetry sweep is the real confirmation.
   */
  const dispatchAction = useCallback(
    (conflictId: string, scenarioId: string) => {
      const action: ControllerAction = {
        event_type: "CONTROLLER_ACTION",
        conflict_id: conflictId,
        scenario_id: scenarioId,
        timestamp: Date.now(),
      };

      const socket = socketRef.current;
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(action));
      } else if (!USE_MOCK) {
        console.error("[telemetry] action dropped, socket not open", action);
        return false;
      }

      store.resolveConflict(conflictId);
      return true;
    },
    [store],
  );

  // Kept because the rest of the app already reaches for it. It is the store's
  // own Map, not a copy -- mutations are visible without a re-render.
  const trainsRef = useRef(store.trains);

  return {
    store,
    trainsRef,
    connection,
    isConnected: connection === "live",
    dispatchAction,
  };
}

// ---------------------------------------------------------------------------
// Channel hooks. Each component subscribes to the narrowest channel it needs,
// so a 10Hz train flush does not re-render the conflict deck.
// ---------------------------------------------------------------------------

function useChannel(store: TelemetryStore, channel: Channel): number {
  return useSyncExternalStore(
    useMemo(() => store.subscribe(channel), [store, channel]),
    useMemo(() => store.getVersion(channel), [store, channel]),
    store.getServerVersion,
  );
}

export function useTrains(store: TelemetryStore): TrackedTrain[] {
  const version = useChannel(store, "trains");
  return useMemo(() => store.roster(), [store, version]);
}

export function useClock(store: TelemetryStore) {
  useChannel(store, "clock");
  return store.clock;
}

export function useConflicts(store: TelemetryStore) {
  const version = useChannel(store, "conflicts");
  return useMemo(
    () => ({
      conflicts: store.openConflicts(),
      recommendations: store.recommendations,
    }),
    [store, version],
  );
}
