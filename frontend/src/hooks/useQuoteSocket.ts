import { useEffect, useRef, useState } from "react";

export interface LiveQuote {
  symbol: string;
  price: number;
  ts: string;
}

// Connects to the backend /ws/quotes stream for a symbol and exposes the latest price.
// Auto-reconnects on drop. Returns the latest quote (or null before the first message).
export function useQuoteSocket(symbol: string, assetClass: string): LiveQuote | null {
  const [quote, setQuote] = useState<LiveQuote | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!symbol) return;
    let closed = false;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${window.location.host}/ws/quotes?symbol=${encodeURIComponent(
        symbol,
      )}&asset_class=${assetClass}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "quote") {
            setQuote({ symbol: msg.symbol, price: msg.price, ts: msg.ts });
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (!closed) reconnectTimer = setTimeout(connect, 2000);
      };
      ws.onerror = () => {
        if (ws.readyState === WebSocket.OPEN) ws.close();
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(reconnectTimer);
      const ws = wsRef.current;
      if (!ws) return;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      // Closing a still-CONNECTING socket logs a console warning — wait for open, then close.
      if (ws.readyState === WebSocket.CONNECTING) {
        ws.onopen = () => ws.close();
      } else {
        ws.onopen = null;
        try {
          ws.close();
        } catch {
          /* already closing/closed */
        }
      }
    };
  }, [symbol, assetClass]);

  return quote;
}
