import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { fmtUsd } from "../format";
import { useLocalStorage } from "../hooks/useLocalStorage";
import { usePolling } from "../hooks/usePolling";
import type { FundsOp } from "../types";

/** The Withdraw page (2026-11-21, the user: "a withdraw option and record here, instead of going to Exness").
 * MT5 cannot move money, so the withdrawal itself is made in the Exness Personal Area (login + code): the page
 * opens it, says how much Exness will pay out now (the free margin), works out what a withdrawal does to the
 * account, and keeps the record of every deposit and withdrawal straight from MT5's balance deals (GET
 * /api/broker/funds) - a new one shows up within seconds of Exness booking it. Read-only. A withdrawal that came
 * straight back (and the deposit that brought it back) is not shown at all (the user, same night: "clean out
 * anything that went wrong and came back"); the totals never counted them. */

// The Personal Area's address. Exness sends a signed-out visitor to its sign-in and then to the Personal Area,
// whose menu has Withdrawal; the user can pin the exact page once (kept in this browser).
const EXNESS_PA = "https://my.exness.com/";
const POLL_MS = 15_000;

const KIND: Record<FundsOp["kind"], { label: string; tone: string; hint: string }> = {
  deposit: { label: "Deposit", tone: "text-bull", hint: "Money you put in" },
  withdrawal: { label: "Withdrawal", tone: "text-bear", hint: "Money you took out" },
  transfer_in: { label: "Transfer in", tone: "text-bull", hint: "From another of your Exness accounts" },
  transfer_out: { label: "Transfer out", tone: "text-bear", hint: "To another of your Exness accounts" },
  protection: {
    label: "Exness reset to $0", tone: "text-sky-300",
    hint: "Negative balance protection: the balance had gone below zero (right before, it was exactly minus this amount) and Exness put it back to $0. Not your money in, and you owe nothing.",
  },
  dividend: {
    label: "Index dividend", tone: "text-neutral-300",
    hint: "The dividend adjustment Exness books on an index CFD held overnight on the day its shares pay a dividend. Part of trading, not money in or out.",
  },
  credit: { label: "Credit / bonus", tone: "text-neutral-300", hint: "MT5 credit or bonus - not withdrawable money" },
  other: { label: "Other", tone: "text-neutral-300", hint: "A balance operation Exness did not word as a deposit or withdrawal" },
};

type Filter = "all" | "out" | "in" | "other";
const FILTERS: { id: Filter; label: string; kinds: FundsOp["kind"][] }[] = [
  { id: "all", label: "All", kinds: [] },
  { id: "out", label: "Withdrawals", kinds: ["withdrawal", "transfer_out"] },
  { id: "in", label: "Deposits", kinds: ["deposit", "transfer_in"] },
  { id: "other", label: "Other", kinds: ["protection", "dividend", "credit", "other"] },
];

function fmtWhen(iso: string): string {
  return new Date(iso).toLocaleString([], { year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fmtUtc(iso: string): string {
  return `${new Date(iso).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function Tile({ label, value, tone = "text-neutral-100", sub, hint }: {
  label: string; value: string; tone?: string; sub?: string; hint?: string;
}) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950/40 px-3 py-2" title={hint}>
      <div className="text-[11px] uppercase tracking-wider text-neutral-500">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold tabular-nums ${tone}`}>{value}</div>
      {sub && <div className="text-[11px] text-neutral-500">{sub}</div>}
    </div>
  );
}

function safeUrl(u: string): string | null {
  try {
    const x = new URL(u.trim());
    return x.protocol === "https:" ? x.toString() : null;
  } catch {
    return null;
  }
}

export function WithdrawView() {
  const { data, error, loading } = usePolling(() => api.funds(), POLL_MS);
  const [link, setLink] = useLocalStorage<string>("ta.withdrawLink", EXNESS_PA);
  const [editLink, setEditLink] = useState(false);
  const [draft, setDraft] = useState("");
  const [amountText, setAmountText] = useState("");
  const [filter, setFilter] = useLocalStorage<Filter>("ta.withdrawFilter", "all");

  // Rows that appear while the page is open (a withdrawal just made on Exness) are lit for a while.
  const seenRef = useRef<Set<number> | null>(null);
  const [fresh, setFresh] = useState<Set<number>>(new Set());
  useEffect(() => {
    if (!data) return;
    const ids = data.ops.map((o) => o.ticket);
    if (seenRef.current == null) {
      seenRef.current = new Set(ids);
      return;
    }
    const added = ids.filter((t) => !seenRef.current!.has(t));
    if (!added.length) return;
    added.forEach((t) => seenRef.current!.add(t));
    setFresh((f) => new Set([...f, ...added]));
  }, [data]);

  const href = safeUrl(link) ?? EXNESS_PA;
  const amount = Number(amountText.replace(/[^0-9.]/g, ""));
  const plan = useMemo(() => {
    if (!data || !(amount > 0)) return null;
    const equity = data.equity - amount;
    return {
      tooMuch: amount > data.withdrawable + 0.005,
      balance: data.balance - amount,
      equity,
      free: data.margin_free - amount,
      level: data.margin > 0 ? (equity / data.margin) * 100 : null,
      smaller: data.equity > 0 ? amount / data.equity : 0,
    };
  }, [data, amount]);

  const rows = useMemo(() => {
    if (!data) return [];
    const kinds = FILTERS.find((f) => f.id === filter)?.kinds ?? [];
    return data.ops.filter((o) => !o.returned && (kinds.length === 0 || kinds.includes(o.kind)));
  }, [data, filter]);

  const tot = data?.totals;
  const cur = data?.currency && data.currency !== "USD" ? ` ${data.currency}` : "";
  const money = (v: number | null | undefined, sign = false) => `${fmtUsd(v, { sign })}${v == null ? "" : cur}`;

  return (
    <div className="space-y-3 p-3">
      {error && !data && (
        <div className="rounded-lg border border-bear/40 bg-bear/10 px-3 py-2 text-sm text-bear">
          {error.startsWith("404")
            ? "The record needs the backend update (the Withdraw page's reader) - it shows after the next backend restart."
            : `Can't read the account's deposits and withdrawals right now: ${error}`}
        </div>
      )}
      {error && data && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-200">
          MT5 can't be read right now - the numbers below are the last read ({fmtWhen(data.read_at)}).
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
        {/* Withdraw: how much, the button, and what it does to the account */}
        <div className="card space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div title="Exness pays out up to the FREE MARGIN: your equity minus the margin the open trades hold.">
              <div className="text-[11px] uppercase tracking-wider text-neutral-500">You can withdraw up to</div>
              <div className="text-3xl font-semibold tabular-nums text-neutral-50">
                {data ? money(data.withdrawable) : loading ? "…" : "—"}
              </div>
              {data && (
                <div className="mt-0.5 text-xs text-neutral-400 tabular-nums">
                  balance {money(data.balance)} · equity {money(data.equity)}
                  {data.open_positions > 0 && (
                    <> · {data.open_positions} open trade{data.open_positions > 1 ? "s" : ""}{" "}
                      <span className={data.floating >= 0 ? "text-bull" : "text-bear"}>{money(data.floating, true)}</span></>
                  )}
                </div>
              )}
            </div>
            <div className="flex flex-col items-end gap-1">
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="btn btn-primary px-4 py-2 text-base"
                title={`Opens ${href} in a new tab. Exness asks for your login and its code there - this app never sees them.`}
              >
                Withdraw on Exness ↗
              </a>
              {!editLink ? (
                <button
                  type="button"
                  className="text-[11px] text-neutral-500 hover:text-neutral-300"
                  onClick={() => { setDraft(link); setEditLink(true); }}
                  title="Open Withdrawal in the Exness Personal Area once, copy that page's address from the browser bar, and pin it here: the button then goes straight to it."
                >
                  {href === EXNESS_PA ? "📌 pin your Exness withdrawal page" : `📌 ${new URL(href).host}${new URL(href).pathname} · change`}
                </button>
              ) : (
                <form
                  className="flex items-center gap-1"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const u = safeUrl(draft);
                    if (u) { setLink(u); setEditLink(false); }
                  }}
                >
                  <input
                    autoFocus
                    className="field w-64 py-0.5 text-xs"
                    placeholder="https://my.exness.com/..."
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <button type="submit" className="btn btn-subtle px-2 py-0.5 text-xs" disabled={!safeUrl(draft)}>Save</button>
                  <button type="button" className="btn btn-ghost px-2 py-0.5 text-xs"
                          onClick={() => { setLink(EXNESS_PA); setEditLink(false); }}>Reset</button>
                </form>
              )}
            </div>
          </div>

          {data && data.open_positions > 0 && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/5 px-2.5 py-1.5 text-xs text-amber-200">
              {data.open_positions} trade{data.open_positions > 1 ? "s are" : " is"} open: the margin {data.open_positions > 1 ? "they hold" : "it holds"} ({money(data.margin)}) can't be
              withdrawn, and taking money out lowers the margin level while {data.open_positions > 1 ? "they run" : "it runs"}. With no trade open you can take out up to the whole balance.
            </div>
          )}

          {/* What a withdrawal does to the account, before you make it */}
          <div className="rounded-md border border-neutral-800 bg-neutral-950/40 p-2.5">
            <label className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-neutral-300">If I withdraw</span>
              <span className="relative">
                <span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-neutral-500">$</span>
                <input
                  inputMode="decimal"
                  className="field w-32 pl-5 tabular-nums"
                  placeholder="0.00"
                  value={amountText}
                  onChange={(e) => setAmountText(e.target.value)}
                />
              </span>
              {data && data.withdrawable > 0 && (
                <button type="button" className="chip hover:border-neutral-500"
                        onClick={() => setAmountText(data.withdrawable.toFixed(2))}>max</button>
              )}
            </label>
            {plan ? (
              <div className="mt-2 space-y-1 text-xs">
                {plan.tooMuch ? (
                  <div className="text-bear">
                    More than the free margin ({money(data!.withdrawable)}): Exness won't pay this out
                    {data!.open_positions > 1 ? " while the open trades hold their margin"
                      : data!.open_positions === 1 ? " while the open trade holds its margin" : ""}.
                  </div>
                ) : (
                  <>
                    <div className="tabular-nums text-neutral-300">
                      after it: balance <b className="text-neutral-100">{money(plan.balance)}</b> · equity {money(plan.equity)} · free margin {money(plan.free)}
                      {plan.level != null && data!.open_positions > 0 && (
                        <> · margin level <b className="text-neutral-100">{Math.round(plan.level).toLocaleString()}%</b>
                          <span className="text-neutral-500"> (now {Math.round(data!.margin_level ?? 0).toLocaleString()}%)</span></>
                      )}
                    </div>
                    <div className="text-neutral-400">
                      The app sizes every trade from the equity, so trades after it are about{" "}
                      <b className="text-neutral-200">{Math.round(plan.smaller * 100)}% smaller</b> - the same risk %, on less money.
                    </div>
                  </>
                )}
              </div>
            ) : (
              <div className="mt-1.5 text-xs text-neutral-500">
                Type an amount to see the account after it. Nothing is sent - the withdrawal is made on Exness.
              </div>
            )}
          </div>

          <div className="text-[11px] leading-relaxed text-neutral-500">
            MT5 can't move money, so the withdrawal itself is asked for in the Exness Personal Area (your login and
            its code stay there). Exness takes it off the account at once: it shows in the record here within{" "}
            {POLL_MS / 1000} s. Exness usually pays back to the method you deposited with first.
          </div>
        </div>

        {/* Money in and out, all time */}
        <div className="card space-y-2">
          <div className="section-label">Money in and out · since the first deposit</div>
          {tot ? (
            <>
              <div className="grid grid-cols-2 gap-2 xl:grid-cols-3">
                <Tile label="Put in" value={money(tot.money_in)} tone="text-bull"
                      sub={tot.transfers_in ? `${money(tot.deposits)} deposits + ${money(tot.transfers_in)} transfers` : undefined}
                      hint="Deposits plus transfers in from your other Exness accounts" />
                <Tile label="Taken out" value={money(tot.money_out)} tone="text-bear"
                      sub={`${tot.withdrawals_n} withdrawal${tot.withdrawals_n === 1 ? "" : "s"}${tot.transfers_out ? ` (${money(tot.transfers_out)} transfers)` : ""}`}
                      hint="Withdrawals plus transfers out to your other Exness accounts." />
                <Tile label="Net put in" value={money(tot.net_in)} hint="Put in minus taken out" />
                <Tile label="Balance now" value={money(data!.balance)} sub={`equity ${money(data!.equity)}`} />
                <Tile
                  label="Trading result"
                  value={money(tot.trading_result, true)}
                  tone={(tot.trading_result ?? 0) >= 0 ? "text-bull" : "text-bear"}
                  sub="balance minus net put in"
                  hint="What the account made or lost from trading since the first deposit: the balance now minus what you put in and haven't taken out. It includes Exness's resets to $0 and the index dividends listed below."
                />
                <Tile
                  label="Last withdrawal"
                  value={tot.last_withdrawal ? money(Math.abs(tot.last_withdrawal.amount)) : "none yet"}
                  sub={tot.last_withdrawal ? `${fmtWhen(tot.last_withdrawal.time)}${tot.last_withdrawal.method ? ` · ${tot.last_withdrawal.method}` : ""}` : undefined}
                />
              </div>
              {(tot.trading_result ?? 0) > 0 && (
                <div className="text-xs text-bull">
                  You could take out the {money(tot.trading_result)} profit and keep the {money(tot.net_in)} you put in.
                </div>
              )}
              <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-neutral-500">
                {tot.protection_n > 0 && (
                  <span title={KIND.protection.hint}>
                    Exness reset a negative balance to $0 {tot.protection_n}× ({money(tot.protection)})
                  </span>
                )}
                {tot.dividends !== 0 && <span title={KIND.dividend.hint}>index dividends {money(tot.dividends, true)}</span>}
              </div>
            </>
          ) : (
            <div className="text-sm text-neutral-500">{loading ? "Reading MT5…" : "—"}</div>
          )}
        </div>
      </div>

      {/* The record */}
      <div className="card">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <div className="card-title mr-1">Record</div>
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              aria-pressed={filter === f.id}
              onClick={() => setFilter(f.id)}
              className={`rounded-full border px-2 py-0.5 text-xs transition ${
                filter === f.id ? "border-neutral-400 bg-neutral-700/60 text-neutral-100" : "border-neutral-700 text-neutral-400 hover:text-neutral-200"
              }`}
            >
              {f.label}
            </button>
          ))}
          <span className="ml-auto text-[11px] text-neutral-500">
            from MT5 · times on your clock{data ? ` · read ${new Date(data.read_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : ""}
          </span>
        </div>
        {rows.length === 0 ? (
          <div className="py-3 text-sm text-neutral-500">{data ? "Nothing here." : loading ? "Reading MT5…" : "—"}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wider text-neutral-500">
                  <th className="py-1 pr-3 font-medium">When</th>
                  <th className="py-1 pr-3 font-medium">What</th>
                  <th className="py-1 pr-3 font-medium">Method</th>
                  <th className="py-1 pr-3 text-right font-medium">Amount</th>
                  <th className="py-1 pr-3 text-right font-medium">Balance after</th>
                  <th className="py-1 font-medium">Exness reference</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((o) => {
                  const k = KIND[o.kind];
                  return (
                    <tr
                      key={o.ticket}
                      className={`border-t border-neutral-800/70 ${fresh.has(o.ticket) ? "bg-amber-400/10" : ""}`}
                    >
                      <td className="py-1.5 pr-3 tabular-nums" title={fmtUtc(o.time)}>{fmtWhen(o.time)}</td>
                      <td className="py-1.5 pr-3">
                        <span className={k.tone} title={k.hint}>{k.label}</span>
                        {fresh.has(o.ticket) && <span className="ml-1.5 rounded bg-amber-400/20 px-1 text-[10px] text-amber-200">new</span>}
                      </td>
                      <td className="py-1.5 pr-3 text-neutral-400">{o.method ?? "—"}</td>
                      <td className={`py-1.5 pr-3 text-right font-medium tabular-nums ${o.amount >= 0 ? "text-bull" : "text-bear"}`}>
                        {money(o.amount, true)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums text-neutral-300">{money(o.balance_after)}</td>
                      <td className="py-1.5 font-mono text-[11px] text-neutral-500">{o.comment || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
