"""The account's money in and out, from the broker's balance deals (2026-11-21, the user: "add me a withdraw option
and record, so I don't have to go to Exness for it").

MT5 cannot move money - a withdrawal is asked for in the Exness Personal Area, which wants the login and a code.
What MT5 does have is every balance operation the moment Exness books it, and Exness words them:

- ``D-<method>-...``   money in (``D-CC-USD-...`` = a card deposit)
- ``W-<method>-...``   money out (``W-CC-USD-...`` = a card withdrawal)
- ``...-ALLINT-...``   a transfer between the client's own Exness accounts
- ``D-NULL``           Exness putting a NEGATIVE balance back to 0 (negative balance protection: on the real
                       account each one came right after the balance had gone to exactly minus that amount)
- ``DIV-<symbol>-...`` the dividend adjustment on an index CFD held overnight - trading, not money in or out

A withdrawal that comes back as a deposit of the same amount and method within minutes (seven times on the real
account, 13 s to 3 min later) left nothing out: both are marked "returned" and counted in neither total.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

RETURN_WINDOW_S = 600      # a deposit of the same amount + method this soon after a withdrawal = it came back

_METHODS = {
    "CC": "card",
    "ALLINT": "Exness account transfer",
}

KINDS = ("deposit", "withdrawal", "transfer_in", "transfer_out", "protection", "dividend", "credit", "other")


def _method(code: str) -> str:
    return _METHODS.get(code, code.lower())


def kind_of(comment: str, amount: float, mt5_type: int | None = None) -> tuple[str, str | None]:
    """(kind, method) of one balance operation from Exness's comment. ``mt5_type`` 3 / 6 = MT5's credit / bonus."""
    c = (comment or "").strip().upper()
    if mt5_type in (3, 6):
        return "credit", None
    if c.startswith("DIV-") or c.startswith("DIV "):
        return "dividend", None
    if c == "D-NULL":
        return "protection", None
    m = re.match(r"^([DW])-([A-Z0-9]+)", c)
    if m:
        code = m.group(2)
        internal = code == "ALLINT"
        if m.group(1) == "D":
            return ("transfer_in" if internal else "deposit"), _method(code)
        return ("transfer_out" if internal else "withdrawal"), _method(code)
    return "other", None


def build(raw_ops: list[dict], account: dict) -> dict:
    """The record + totals. ``raw_ops`` = [{ticket, time (tz-aware), amount, comment, mt5_type?, balance_after?}]
    oldest first or not; ``account`` = {currency, balance, equity, margin, margin_free, margin_level,
    open_positions}."""
    ops = []
    for r in sorted(raw_ops, key=lambda x: (x["time"], x["ticket"])):
        kind, method = kind_of(r.get("comment") or "", r["amount"], r.get("mt5_type"))
        t = r["time"] if r["time"].tzinfo else r["time"].replace(tzinfo=timezone.utc)
        ops.append({
            "ticket": int(r["ticket"]),
            "time": t.isoformat(),
            "_t": t,
            "amount": round(float(r["amount"]), 2),
            "kind": kind,
            "method": method,
            "comment": r.get("comment") or "",
            # + 0.0: a balance put back to 0 sums to -0.0, which would show as "-$0.00"
            "balance_after": None if r.get("balance_after") is None else round(float(r["balance_after"]), 2) + 0.0,
            "returned": False,
            "pair": None,           # the ticket of the other half of a returned withdrawal
            "back_after_s": None,
        })
    # A withdrawal followed by a deposit of the same amount and method within minutes: it came back.
    for i, w in enumerate(ops):
        if w["kind"] != "withdrawal" or w["returned"]:
            continue
        for d in ops[i + 1:]:
            gap = (d["_t"] - w["_t"]).total_seconds()
            if gap > RETURN_WINDOW_S:
                break
            if (d["kind"] == "deposit" and not d["returned"] and d["method"] == w["method"]
                    and abs(d["amount"] + w["amount"]) < 0.005):
                w["returned"] = d["returned"] = True
                w["pair"], d["pair"] = d["ticket"], w["ticket"]
                w["back_after_s"] = d["back_after_s"] = int(gap)
                break

    def total(kind: str) -> float:
        return round(sum(abs(o["amount"]) for o in ops if o["kind"] == kind and not o["returned"]), 2)

    deposits, withdrawals = total("deposit"), total("withdrawal")
    t_in, t_out = total("transfer_in"), total("transfer_out")
    money_in = round(deposits + t_in, 2)
    money_out = round(withdrawals + t_out, 2)
    net_in = round(money_in - money_out, 2)
    balance = float(account.get("balance") or 0)
    equity = float(account.get("equity") or 0)
    real_out = [o for o in ops if o["kind"] in ("withdrawal", "transfer_out") and not o["returned"]]
    last_out = real_out[-1] if real_out else None
    for o in ops:
        o.pop("_t")
    return {
        **account,
        "ops": list(reversed(ops)),          # newest first, as the page lists them
        "totals": {
            "deposits": deposits,
            "withdrawals": withdrawals,
            "transfers_in": t_in,
            "transfers_out": t_out,
            "money_in": money_in,
            "money_out": money_out,
            "net_in": net_in,
            "protection": total("protection"),
            "protection_n": sum(1 for o in ops if o["kind"] == "protection"),
            "dividends": round(sum(o["amount"] for o in ops if o["kind"] == "dividend"), 2),
            "returned_n": sum(1 for o in ops if o["returned"] and o["kind"] == "withdrawal"),
            # what the account made or lost trading since its first deposit: what is there minus what was put in
            "trading_result": round(balance - net_in, 2) if ops else None,
            "trading_result_equity": round(equity - net_in, 2) if ops else None,
            "withdrawals_n": len(real_out),
            "last_withdrawal": None if last_out is None else {"time": last_out["time"], "amount": last_out["amount"],
                                                              "method": last_out["method"]},
        },
        # Exness pays out up to the free margin (equity minus the margin the open trades hold).
        "withdrawable": round(max(0.0, float(account.get("margin_free") or 0)), 2),
        "read_at": datetime.now(timezone.utc).isoformat(),
    }
