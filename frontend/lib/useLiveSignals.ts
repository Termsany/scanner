"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { wsUrl } from "./api";
import type { Chain, LiveSignal } from "./types";

export type SocketState = "connecting" | "open" | "closed";

interface Options {
  chains?: Chain[] | null;
  minScore?: number;
  onSignal?: (s: LiveSignal) => void;
  onScanComplete?: (runId: number) => void;
}

const MAX_FEED = 40;

/**
 * Native WebSocket client for /ws/live-signals with exponential-backoff reconnect,
 * server-side filter subscription and a capped in-memory feed.
 */
export function useLiveSignals({ chains = null, minScore = 75, onSignal, onScanComplete }: Options) {
  const [state, setState] = useState<SocketState>("connecting");
  const [feed, setFeed] = useState<LiveSignal[]>([]);
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const filterRef = useRef({ chains, minScore });
  const handlers = useRef({ onSignal, onScanComplete });
  handlers.current = { onSignal, onScanComplete };

  const sendSubscribe = useCallback(() => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      const { chains: c, minScore: m } = filterRef.current;
      ws.send(JSON.stringify({ type: "subscribe", chains: c && c.length ? c : [], min_score: m }));
    }
  }, []);

  useEffect(() => {
    filterRef.current = { chains, minScore };
    sendSubscribe();
  }, [chains, minScore, sendSubscribe]);

  useEffect(() => {
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      setState("connecting");
      const ws = new WebSocket(wsUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        setState("open");
        sendSubscribe();
      };

      ws.onmessage = (ev) => {
        setLastMessageAt(Date.now());
        let msg: { type: string; [k: string]: unknown };
        try {
          msg = JSON.parse(ev.data as string);
        } catch {
          return;
        }
        if (msg.type === "hello") {
          const recent = (msg.recent as LiveSignal[] | undefined) ?? [];
          setFeed(recent.slice(0, MAX_FEED));
        } else if (msg.type === "signal") {
          const sig = msg as unknown as LiveSignal;
          setFeed((prev) => {
            if (sig.signal_id && prev.some((p) => p.signal_id === sig.signal_id)) return prev;
            return [sig, ...prev].slice(0, MAX_FEED);
          });
          handlers.current.onSignal?.(sig);
        } else if (msg.type === "scan_complete") {
          handlers.current.onScanComplete?.(msg.run_id as number);
        }
      };

      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (disposed) return;
        setState("closed");
        const delay = Math.min(30_000, 1_000 * 2 ** attempt) + Math.random() * 500;
        attempt += 1;
        timer = setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, [sendSubscribe]);

  return { state, feed, lastMessageAt };
}
