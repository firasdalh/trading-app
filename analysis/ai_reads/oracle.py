"""Stand-in for the three AI reads during a replay: same prompt text, answer from a label store or forced.

The engine imports ``interpret_momentum`` / ``interpret_regime`` / ``interpret_price_action`` inside the
fork functions at call time, so replacing the module attributes routes every fork through here. Each
call builds the exact prompt the live model would get (the modules' own ``_snapshot``) and keys it by a
hash of system + user text, so a label fetched once answers that same decision in every replay.
"""
import hashlib

import app.agents.momentum_read as MR
import app.agents.priceaction_read as PR
import app.agents.regime_read as RR

SCHEMAS = {"momentum": (MR.MomentumRead, MR._SYSTEM), "regime": (RR.RegimeRead, RR._SYSTEM),
           "priceaction": (PR.PriceActionRead, PR._SYSTEM)}


def key_of(system: str, user: str) -> str:
    return hashlib.sha1((system + "\n" + user).encode()).hexdigest()[:20]


class Oracle:
    def __init__(self):
        self.forced: dict | None = None   # fork -> category to return (exploration), else use labels
        self.labels: dict = {}            # key -> (category, confidence, evidence)
        self.seen: list = []              # (fork, key) reached by the current decision
        self.texts: dict = {}             # key -> (fork, user text)
        self.misses = 0

    def answer(self, fork: str, user: str):
        schema, system = SCHEMAS[fork]
        key = key_of(system, user)
        self.seen.append((fork, key))
        self.texts.setdefault(key, (fork, user))
        if self.forced is not None:
            cat = self.forced.get(fork)
            return None if cat is None else schema(category=cat, evidence="forced", confidence=1.0)
        lab = self.labels.get(key)
        if lab is None:
            self.misses += 1
            return None
        return schema(category=lab[0], confidence=lab[1], evidence=lab[2])


ORACLE = Oracle()


def install() -> Oracle:
    MR.interpret_momentum = lambda symbol, direction, ind, technical, tf: ORACLE.answer(
        "momentum", MR._snapshot(symbol, direction, ind, technical, tf))
    RR.interpret_regime = lambda symbol, ind, technical, tf: ORACLE.answer(
        "regime", RR._snapshot(symbol, ind, technical, tf))
    PR.interpret_price_action = lambda symbol, direction, level, ind, tf: ORACLE.answer(
        "priceaction", PR._snapshot(symbol, direction, level, ind, tf))
    return ORACLE
