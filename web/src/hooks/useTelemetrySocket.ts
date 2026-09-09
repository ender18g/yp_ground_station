import { useEffect, useRef, useState } from "react";
import { websocketUrl } from "../api";

interface UseTelemetrySocketOptions {
  enabled: boolean;
  onPayload: (payload: Record<string, unknown>) => void;
  onAuthenticationExpired: () => void;
}

export function useTelemetrySocket({ enabled, onPayload, onAuthenticationExpired }: UseTelemetrySocketOptions) {
  const socketRef = useRef<WebSocket | null>(null);
  const payloadRef = useRef(onPayload);
  const authExpiredRef = useRef(onAuthenticationExpired);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    payloadRef.current = onPayload;
    authExpiredRef.current = onAuthenticationExpired;
  }, [onPayload, onAuthenticationExpired]);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let retry: number | undefined;

    const connect = () => {
      if (disposed) return;
      const socket = new WebSocket(websocketUrl("/ws/ui"));
      socketRef.current = socket;
      socket.onopen = () => setConnected(true);
      socket.onclose = (event) => {
        setConnected(false);
        if (disposed) return;
        if (event.code === 4001) {
          authExpiredRef.current();
          return;
        }
        retry = window.setTimeout(connect, 1500);
      };
      socket.onerror = () => setConnected(false);
      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data) as unknown;
          if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
          payloadRef.current(payload as Record<string, unknown>);
        } catch {
          // Ignore malformed telemetry frames and keep the connection alive.
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(retry);
      socketRef.current?.close();
      socketRef.current = null;
      setConnected(false);
    };
  }, [enabled]);

  return { connected, socketRef };
}
