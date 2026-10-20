import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import type { GroupStat } from "../types";

// One fetch shared by every panel on screen (the breakdown is the same data for all of them).
let pending: Promise<GroupStat[]> | null = null;
let fetchedAt = 0;
function bySource(): Promise<GroupStat[]> {
  if (!pending || Date.now() - fetchedAt > 60_000) {
    fetchedAt = Date.now();
    pending = api.journalBreakdown().then((b) => b.by_source).catch(() => []);
  }
  return pending;
}

/** Sum the journal rows of the given sources into one record. */
export function combine(rows: GroupStat[], sources: string[]): { trades: number; net: number; avgR: number | null } {
  const mine = rows.filter((r) => sources.includes(r.label));
  const trades = mine.reduce((a, r) => a + r.trades, 0);
  const net = mine.reduce((a, r) => a + r.net_pnl, 0);
  const rTrades = mine.filter((r) => r.total_r != null);
  const totalR = rTrades.reduce((a, r) => a + (r.total_r ?? 0), 0);
  const rCount = rTrades.reduce((a, r) => a + r.trades, 0);
  return { trades, net, avgR: rCount ? totalR / rCount : null };
}

/** Load the combined record of some sources (for confirm dialogs). */
export async function sourceRecord(sources: string[]) {
  return combine(await bySource(), sources);
}

/**
 * "Your record" for an automated opener, shown next to the switch that turns it on.
 *
 * The journal already had each opener's live result, but only on the Journal page - not where the
 * decision to run it is made. The auto-trader lost $917 over 27 trades and was one click from
 * running again with no hint of that (full review 2026-10-20). Red when avg R is negative over at
 * least 20 trades; below 20 it is labelled too few to judge.
 */
export function SourceRecord({ sources, label = "Your record" }: { sources: string[]; label?: string }) {
  const [rows, setRows] = useState<GroupStat[] | null>(null);
  useEffect(() => {
    let dead = false;
    void bySource().then((r) => { if (!dead) setRows(r); });
    return () => { dead = true; };
  }, []);
  if (!rows) return null;
  const { trades, net, avgR } = combine(rows, sources);
  if (!trades) {
    return <div className="text-[11px] text-neutral-500">{label}: no closed trades yet</div>;
  }
  const losing = avgR != null && avgR < 0 && trades >= 20;
  const few = trades < 20;
  return (
    <div
      className={`text-[11px] ${losing ? "font-semibold text-bear" : "text-neutral-400"}`}
      title="Closed trades opened by this mechanism, from the journal (since the last journal reset)."
    >
      {label}: {trades} trades · avg {avgR == null ? "—" : `${avgR >= 0 ? "+" : ""}${avgR.toFixed(2)}R`} · net{" "}
      {fmtUsd(net, { sign: true })}
      {few && <span className="ml-1 font-normal text-neutral-600">(too few to judge)</span>}
      {losing && <span className="ml-1">- losing so far</span>}
    </div>
  );
}
