import { useEffect, useRef, useState } from "react";

// Polls an async function on an interval and exposes data/error/loading.
// Used for account, positions, risk state, and proposals (low-frequency state).
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number, deps: unknown[] = [],
                              opts: { whileHidden?: boolean } = {}) {
  // `whileHidden`: keep polling with the window hidden - for what must reach you when you are NOT
  // looking (the approval chime). Everything else pauses.
  const whileHidden = !!opts.whileHidden;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const loadedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval>;

    const tick = async () => {
      // Nothing is looking: skip. About 114 requests a minute kept running with the window hidden,
      // each competing with real orders for the single MT5 connection (full review 2026-10-20).
      // The first load still runs, and a tick fires the moment the window is shown again.
      if (!whileHidden && document.visibilityState === "hidden" && loadedRef.current) return;
      try {
        const result = await fnRef.current();
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
        loadedRef.current = true;
      }
    };

    tick();
    timer = setInterval(tick, intervalMs);
    const onShow = () => { if (document.visibilityState === "visible") void tick(); };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onShow);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading };
}
