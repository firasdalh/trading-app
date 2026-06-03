import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AdvisorActivityItem } from "../types";
import { actionText, ago, notifyNew } from "./advisorFormat";

interface Props {
  // Bump to force a refresh (e.g. after a position is closed/opened).
  refreshSignal?: number;
}

// Timeline of what the advisor actually DID (close / protect / trail / re-enter), newest first.
// Polls independently so it stays current even while the advisor runs headless, and fires
// browser notifications for newly-executed actions.
export function AdvisorActivity({ refreshSignal }: Props) {
  const [items, setItems] = useState<AdvisorActivityItem[]>([]);

  useEffect(() => {
    let cancelled = false;
    const fetchActivity = async () => {
      const acts = await api.advisorActivity().catch(() => [] as AdvisorActivityItem[]);
      if (cancelled) return;
      setItems(acts);
      notifyNew(acts);
    };
    void fetchActivity();
    const id = setInterval(fetchActivity, 20_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [refreshSignal]);

  if (items.length === 0) return null;

  return (
    <div className="card">
      <div className="mb-2 text-sm font-semibold">Recent actions</div>
      <ul className="space-y-1">
        {items.slice(0, 10).map((it) => (
          <li key={`${it.run_id}-${it.seq}`} className="flex items-center gap-2 text-xs">
            <span className={it.ok ? "text-bull" : it.action === "close_pending" ? "text-warn" : "text-bear"}>
              {it.ok ? "✓" : it.action === "close_pending" ? "⏳" : "✗"}
            </span>
            <span className="font-medium">{it.symbol}</span>
            <span className="text-neutral-300">{actionText(it)}</span>
            <span className="ml-auto whitespace-nowrap text-neutral-500">{ago(it.at)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
