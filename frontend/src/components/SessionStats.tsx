import { useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import { usePolling } from "../hooks/usePolling";
import { localHour, sessionAtHour, sessionByKey, utcHour } from "../sessions";
import type { SessionSourceStat, SessionStat } from "../types";
import { srcColor, srcLabel } from "./sourceMeta";

/**
 * The journal by trading session (asked 2026-11-10: "if a deal closed, win or lose, and in which session - Asia,
 * Middle East, New York or London - so I know which session is performing with us better").
 * The trades are the trade log's own (your broker's history), each counted in the session it OPENED in -
 * backend GET /api/journal/sessions, the hours in app/core/sessions.py and ../sessions.ts.
 */

// Below this many trades a session is not ranked: a handful of lucky trades would crown it.
const MIN_RANKED = 10;
const EMPTY: SessionStat = { trades: 0, wins: 0, losses: 0, win_rate: null, net_pnl: 0, avg_pnl: null, profit_factor: null };

const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const pf = (s: SessionStat) =>
  s.profit_factor != null ? s.profit_factor.toFixed(2) : s.wins > 0 && s.losses === 0 ? "∞" : "—";
const tone = (v: number) => (v > 0 ? "text-bull" : v < 0 ? "text-bear" : "text-neutral-400");
// "+42", "−7", "+0.5", "+1.2k": a cell of the hour strip.
const short = (v: number) => {
  const a = Math.abs(v);
  const body = a >= 1000 ? `${(a / 1000).toFixed(1)}k` : a >= 10 ? String(Math.round(a)) : a.toFixed(1);
  return `${v < 0 ? "−" : "+"}${body}`;
};
const hhmm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** The session a trade opened in, as a small coloured tag (the journal's trade log). */
export function SessionTag({ session, openedAt }: { session?: string | null; openedAt: string | null }) {
  const s = sessionByKey(session);
  if (!s) return <span className="text-neutral-600">—</span>;
  return (
    <span
      className="inline-flex items-center gap-1 text-xs"
      style={{ color: `rgb(${s.rgb})` }}
      title={`${openedAt ? `Opened ${hhmm(openedAt)} your time - ` : ""}${s.name}: ${localHour(s.from)}-${localHour(s.to % 24)} your time (${utcHour(s.from)}-${utcHour(s.to)} UTC). ${s.about}`}
    >
      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: `rgb(${s.rgb})` }} />
      {s.name}
    </span>
  );
}

/** A closed trade's result in a word (the journal's trade log). */
export function ResultTag({ pnl }: { pnl: number | null }) {
  if (pnl == null) return <span className="text-neutral-600">—</span>;
  const [text, cls] = pnl > 0 ? ["WIN", "bg-bull/15 text-bull"] : pnl < 0 ? ["LOSS", "bg-bear/15 text-bear"] : ["EVEN", "bg-neutral-800 text-neutral-400"];
  return <span className={`rounded px-1.5 py-px text-[11px] font-bold tracking-wide ${cls}`}>{text}</span>;
}

export function SessionStatsCard() {
  const { data } = usePolling(() => api.journalSessions(), 30000, []);
  const [who, setWho] = useState("all");
  if (!data || data.trades === 0) return null;

  const pick = (total: SessionStat, sources: SessionSourceStat[]) =>
    who === "all" ? total : sources.find((x) => x.source === who)?.stat ?? EMPTY;
  // Who opened them, the most trades first: the filter's choices.
  const counts = new Map<string, number>();
  for (const s of data.sessions) for (const x of s.sources) counts.set(x.source, (counts.get(x.source) ?? 0) + x.stat.trades);
  const whos: [string, number][] = [["all", data.trades], ...[...counts.entries()].sort((a, b) => b[1] - a[1])];

  const rows = data.sessions.map((s) => ({ s, rgb: sessionByKey(s.key)?.rgb ?? "163,163,163", stat: pick(s.total, s.sources) }));
  const shown = rows.reduce((n, r) => n + r.stat.trades, 0);
  const ranked = rows.filter((r) => r.stat.trades >= MIN_RANKED);
  const best = ranked.length >= 2 ? ranked.reduce((a, b) => (b.stat.net_pnl > a.stat.net_pnl ? b : a)) : null;
  const worst = ranked.length >= 2 ? ranked.reduce((a, b) => (b.stat.net_pnl < a.stat.net_pnl ? b : a)) : null;
  const most = Math.max(1, ...rows.map((r) => r.stat.trades));

  // The hours on the viewer's clock: each UTC hour placed where it starts locally.
  const offset = -new Date().getTimezoneOffset();
  const hours = data.hours
    .map((h) => ({ ...h, stat: pick(h.total, h.sources), at: (((h.hour_utc * 60 + offset) % 1440) + 1440) % 1440 }))
    .sort((a, b) => a.at - b.at);
  const maxAbs = Math.max(1, ...hours.map((h) => Math.abs(h.stat.net_pnl)));
  const since = data.since ? new Date(data.since).toLocaleDateString([], { day: "numeric", month: "short" }) : null;

  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="text-sm font-semibold">Sessions — which one pays</span>
        <span className="text-xs text-neutral-500">
          {shown} closed trade{shown === 1 ? "" : "s"}{since ? ` since ${since}` : ""} from your broker's history, each
          counted in the session it opened in
        </span>
        <div className="ml-auto flex flex-wrap gap-1" role="group" aria-label="Whose trades">
          {whos.map(([k, n]) => (
            <button
              key={k}
              onClick={() => setWho(k)}
              className={`rounded px-2 py-0.5 text-xs ${who === k ? "bg-neutral-700 text-white" : "bg-neutral-900 text-neutral-400 hover:text-neutral-200"}`}
              title={k === "all" ? "Every closed trade" : `Only the trades opened by ${srcLabel(k)}`}
            >
              <span className={k === "all" || who === k ? "" : srcColor(k)}>{k === "all" ? "All" : srcLabel(k)}</span>{" "}
              <span className="tabular-nums text-neutral-500">{n}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full whitespace-nowrap text-sm">
          <thead className="text-left text-xs text-neutral-400">
            <tr>
              <th className="py-1 pr-3">Session <span className="font-normal text-neutral-600">· your time</span></th>
              <th className="pr-3 text-right">Trades</th>
              <th className="pr-3">Won / lost</th>
              <th className="pr-3 text-right">Win rate</th>
              <th className="pr-3 text-right">Net P&amp;L</th>
              <th className="pr-3 text-right">Avg a trade</th>
              <th className="pr-3 text-right" title="What the winners made ÷ what the losers lost. Over 1 = the session makes money.">Profit factor</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ s, rgb, stat }) => (
              <tr key={s.key} className={`border-t border-neutral-800 ${stat.trades ? "" : "text-neutral-600"}`}>
                <td className="py-1.5 pr-3" title={`${utcHour(s.start_utc)}-${utcHour(s.end_utc)} UTC. ${s.about}`}>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: `rgb(${rgb})` }} />
                    <span className="font-medium text-neutral-100">{s.name}</span>
                    <span className="text-xs tabular-nums text-neutral-500">{localHour(s.start_utc)}–{localHour(s.end_utc % 24)}</span>
                  </span>
                </td>
                <td className="pr-3 text-right tabular-nums">{stat.trades}</td>
                <td className="pr-3">
                  {stat.trades ? (
                    <span className="inline-flex items-center gap-1.5 text-xs tabular-nums">
                      <span className="w-9 text-bull">{stat.wins} W</span>
                      <span className="w-9 text-bear">{stat.losses} L</span>
                      {/* The split as a bar, as long as the session's share of the trades. */}
                      <span className="flex h-1.5 overflow-hidden rounded-full bg-neutral-800" style={{ width: `${Math.max(8, (stat.trades / most) * 96)}px` }}>
                        <span className="bg-bull" style={{ width: `${(stat.wins / stat.trades) * 100}%` }} />
                        <span className="bg-bear" style={{ width: `${(stat.losses / stat.trades) * 100}%` }} />
                      </span>
                    </span>
                  ) : "—"}
                </td>
                <td className="pr-3 text-right tabular-nums">{pct(stat.win_rate)}</td>
                <td className={`pr-3 text-right font-semibold tabular-nums ${tone(stat.net_pnl)}`}>
                  {stat.trades ? fmtUsd(stat.net_pnl, { sign: true }) : "—"}
                </td>
                <td className={`pr-3 text-right tabular-nums ${tone(stat.avg_pnl ?? 0)}`}>
                  {stat.avg_pnl == null ? "—" : fmtUsd(stat.avg_pnl, { sign: true })}
                </td>
                <td className="pr-3 text-right tabular-nums">{pf(stat)}</td>
                <td className="text-xs">
                  {best && s.key === best.s.key && <span className="rounded bg-bull/15 px-1.5 py-px font-semibold text-bull">★ best</span>}
                  {worst && worst !== best && s.key === worst.s.key && <span className="rounded bg-bear/10 px-1.5 py-px text-bear">weakest</span>}
                  {stat.trades > 0 && stat.trades < MIN_RANKED && (
                    <span className="text-neutral-600" title={`Under ${MIN_RANKED} trades: too few to rank`}>few trades</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Inside each session, which hour pays. */}
      <div>
        <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-500">By the hour it opened · your time</div>
        <div className="flex gap-0.5">
          {hours.map((h) => {
            const def = sessionAtHour(h.hour_utc);
            const st = h.stat;
            const a = st.trades ? 0.15 + (0.7 * Math.abs(st.net_pnl)) / maxAbs : 0;
            return (
              <div
                key={h.hour_utc}
                className="min-w-0 flex-1"
                title={`${localHour(h.hour_utc)}–${localHour((h.hour_utc + 1) % 24)} your time (${utcHour(h.hour_utc)} UTC) · ${def.name}\n${
                  st.trades
                    ? `${st.trades} trade${st.trades === 1 ? "" : "s"}: ${st.wins} won, ${st.losses} lost (${pct(st.win_rate)})\nNet ${fmtUsd(st.net_pnl, { sign: true })} · avg ${fmtUsd(st.avg_pnl ?? 0, { sign: true })} a trade`
                    : "no trades opened in this hour"}`}
              >
                <div className="h-1 rounded-t-sm" style={{ backgroundColor: `rgba(${def.rgb},0.85)` }} />
                <div
                  className="flex h-8 items-center justify-center rounded-b-sm text-[10px] font-semibold tabular-nums text-white/90"
                  style={{ backgroundColor: st.trades ? `rgba(${st.net_pnl >= 0 ? "34,197,94" : "239,68,68"},${a.toFixed(2)})` : "rgb(23,23,23)" }}
                >
                  {st.trades ? short(st.net_pnl) : ""}
                </div>
                <div className="mt-0.5 text-center text-[9px] tabular-nums text-neutral-500">{localHour(h.hour_utc).slice(0, 2)}</div>
              </div>
            );
          })}
        </div>
      </div>
      <p className="text-[11px] text-neutral-600">
        In $ as your broker booked them, swap and commission included. A trade counts in the session it opened in.
        ★ best needs {MIN_RANKED}+ trades in a session. Middle East = the Gulf markets' first hours, before London.
        {data.skipped > 0 && ` ${data.skipped} trade${data.skipped === 1 ? "" : "s"} left out: no open time or result.`}
      </p>
    </div>
  );
}
