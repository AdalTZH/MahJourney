"use client";

/**
 * NotificationListener — connects to the backend events WebSocket and surfaces
 * live operational alerts (e.g. driver breakdown reports) as toast pop-ups.
 *
 * Mounted once inside Providers so it persists across page navigation and a
 * single connection serves the whole dispatcher UI. The socket lives at
 * "/ws/events" on the same host (Caddy proxies /ws/*), mirroring the voice
 * pipeline's URL derivation. Reconnects automatically with backoff if the
 * connection drops.
 */

import { useEffect } from "react";
import { toast } from "@/components/ui/toast";

type IncomingEvent = {
  type: string;
  driver_id?: string;
  vehicle_id?: string | null;
  description?: string | null;
  location?: string | null;
  remaining_stops?: number;
  at?: string;
};

function eventsSocketUrl(): string {
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/events`;
}

export function NotificationListener() {
  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let closed = false;
    // Exponential backoff on reconnect, capped, reset on a clean open.
    let backoffMs = 1000;

    const connect = () => {
      if (closed) return;
      const url = eventsSocketUrl();
      if (!url) return;
      socket = new WebSocket(url);

      socket.onopen = () => {
        backoffMs = 1000; // reset backoff once connected
      };

      socket.onmessage = (message) => {
        let event: IncomingEvent;
        try {
          event = JSON.parse(message.data);
        } catch {
          return;
        }
        if (event.type === "heartbeat") return;
        if (event.type === "DRIVER_BREAKDOWN") {
          const vehicle = event.vehicle_id ? ` (${event.vehicle_id})` : "";
          const location = event.location && event.location !== "Not specified"
            ? ` near ${event.location}`
            : "";
          const stops = typeof event.remaining_stops === "number"
            ? ` · ${event.remaining_stops} stop(s) remaining`
            : "";
          toast.add({
            title: `Vehicle breakdown — Driver ${event.driver_id}${vehicle}`,
            description: `${event.description ?? "Breakdown reported"}${location}.${stops}`,
            type: "error",
            // Stays until the dispatcher dismisses it — a breakdown shouldn't
            // silently disappear before it's seen.
            timeout: 0,
          });
        }
      };

      socket.onclose = () => {
        if (closed) return;
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 15000);
      };

      socket.onerror = () => {
        // Let onclose handle the reconnect; just close the broken socket.
        socket?.close();
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, []);

  return null;
}
