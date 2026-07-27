import type { AdvisorActivityItem } from "../types";

// Relative "x ago" from an ISO timestamp. Treats a timezone-less string as UTC (not local).
export function ago(iso: string | null): string {
  if (!iso) return "never";
  const safe = /[Z+]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const secs = Math.max(0, Math.round((Date.now() - new Date(safe).getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

// Relative "in x" for a FUTURE ISO timestamp (e.g. an expiry); "expired" once it's past.
export function until(iso: string | null): string {
  if (!iso) return "";
  const safe = /[Z+]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  const secs = Math.round((new Date(safe).getTime() - Date.now()) / 1000);
  if (secs <= 0) return "expired";
  if (secs < 60) return `in ${secs}s`;
  if (secs < 3600) return `in ${Math.round(secs / 60)}m`;
  return `in ${Math.round(secs / 3600)}h`;
}

// Absolute local timestamp for a tooltip (so hovering shows the exact time).
export function localTime(iso: string | null): string {
  if (!iso) return "";
  const safe = /[Z+]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
  return new Date(safe).toLocaleString();
}

// Human label for an advisor action/kind.
export function actionText(a: { action: string; kind?: string | null; stop?: number | null; reason?: string | null }): string {
  if (a.kind === "time_stop") return "closed stagnant trade (time-stop)";
  if (a.action === "close" || a.kind === "close") return "closed position";
  if (a.action === "close_pending") return "close pending confirmation";
  if (a.action === "stop_deferred") return a.reason || "stop change deferred (market closed)";
  if (a.action === "reenter") return "re-entered (new analyzed trade)";
  if (a.action === "reenter_skip") return "re-checked — no fresh setup, stayed flat";
  if (a.action === "reenter_blocked") return "re-entry blocked";
  const at = a.stop != null ? ` @ ${a.stop}` : "";
  if (a.action === "run_target" || a.kind === "run") return `letting winner run — removed target, trailing${at}`;
  if (a.kind === "protect") return `attached protective stop${at}`;
  if (a.kind === "breakeven") return `moved stop → breakeven${at}`;
  if (a.kind === "trail") return `trailed stop${at}`;
  return a.action;
}

const SEEN_KEY = "ta.advisorSeenRun";
const key = (it: AdvisorActivityItem) => it.run_id * 1000 + it.seq;

// Fire a browser notification for newly-executed actions (so headless auto-execute reaches you).
export function notifyNew(items: AdvisorActivityItem[]) {
  if (!items.length) return;
  const maxKey = Math.max(...items.map(key));
  const seen = Number(localStorage.getItem(SEEN_KEY) || 0);
  if (seen === 0) {
    localStorage.setItem(SEEN_KEY, String(maxKey)); // baseline on first load — no backlog spam
    return;
  }
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    for (const it of items.filter((i) => key(i) > seen && i.ok).slice(0, 3)) {
      new Notification("Position advisor", {
        body: `${it.symbol}: ${actionText(it)}${it.reason ? ` — ${it.reason}` : ""}`,
      });
    }
  }
  localStorage.setItem(SEEN_KEY, String(Math.max(maxKey, seen)));
}
