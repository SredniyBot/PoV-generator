import { useEffect, useMemo, useRef, useState } from "react";

import { createProjectSocket } from "./api";
import type { ProjectionName, WsMessage } from "./types";

export type RealtimeStatus = "idle" | "connecting" | "connected" | "degraded";

export interface UseProjectRealtimeOptions {
  projectId: string | null;
  projections?: ProjectionName[];
  onProjectionChanged: (projection: ProjectionName) => void;
}

export function useProjectRealtime({
  projectId,
  projections,
  onProjectionChanged,
}: UseProjectRealtimeOptions): { status: RealtimeStatus; lastChangedProjection: ProjectionName | null } {
  const [status, setStatus] = useState<RealtimeStatus>("idle");
  const [lastChangedProjection, setLastChangedProjection] = useState<ProjectionName | null>(null);
  const retryRef = useRef<number | null>(null);
  const callbackRef = useRef(onProjectionChanged);

  useEffect(() => {
    callbackRef.current = onProjectionChanged;
  }, [onProjectionChanged]);

  const projectionKey = useMemo(() => (projections ?? []).join(","), [projections]);

  useEffect(() => {
    if (!projectId) {
      setStatus("idle");
      setLastChangedProjection(null);
      return;
    }

    let isMounted = true;
    let socket: WebSocket | null = null;

    const connect = () => {
      if (!isMounted) {
        return;
      }
      setStatus("connecting");
      socket = createProjectSocket(projectId, projections);

      socket.onopen = () => {
        if (!isMounted) {
          return;
        }
        setStatus("connected");
      };

      socket.onmessage = (event) => {
        if (!isMounted) {
          return;
        }
        const message = JSON.parse(event.data) as WsMessage;
        if (message.type === "projection_changed") {
          setLastChangedProjection(message.projection);
          callbackRef.current(message.projection);
        }
      };

      socket.onerror = () => {
        if (!isMounted) {
          return;
        }
        setStatus("degraded");
      };

      socket.onclose = () => {
        if (!isMounted) {
          return;
        }
        setStatus("degraded");
        retryRef.current = window.setTimeout(connect, 1800);
      };
    };

    connect();

    return () => {
      isMounted = false;
      if (retryRef.current !== null) {
        window.clearTimeout(retryRef.current);
      }
      socket?.close();
    };
  }, [projectId, projectionKey, projections]);

  return { status, lastChangedProjection };
}
