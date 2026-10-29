"""Pass 2: ask the live model about each decision explore.py found, with the app's exact prompt and settings.

Mirrors ``app.agents.llm._openai_analyze`` (same model, messages, schema, token budget, reasoning effort)
but keeps the token usage so the spend can be reported. Each decision is asked ``--draws`` times
independently: the model in use is a reasoning model that can't be pinned to temperature 0, so a second
draw measures how often the same question gets a different answer. Transient API errors are retried
(they are infrastructure, not the model's judgement); an answer that doesn't parse is kept as "none",
which live falls back on exactly the same way.

Usage (from backend/):  python ../analysis/ai_reads/label.py <needed.json> <labels.jsonl> [--draws 2]
Resumable: answers already in <labels.jsonl> are skipped.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.agents.llm import _is_reasoning_model, _openai_client  # noqa: E402
from app.agents.llm_config import resolve_llm_config  # noqa: E402

from oracle import SCHEMAS  # noqa: E402

PRICE_IN, PRICE_OUT = 0.25, 2.00   # gpt-5-mini, $ per 1M tokens
_MAX_TOKENS = 500                  # what interpret_momentum / _regime / _price_action pass


def ask(client, model, fork, user):
    schema, system = SCHEMAS[fork]
    kwargs = dict(model=model, response_format=schema, max_completion_tokens=max(_MAX_TOKENS, 8000),
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if _is_reasoning_model(model):
        resp = client.beta.chat.completions.parse(reasoning_effort="low", **kwargs)
    else:
        resp = client.beta.chat.completions.parse(temperature=0, seed=7, **kwargs)
    parsed = resp.choices[0].message.parsed
    u = resp.usage
    det = getattr(u, "completion_tokens_details", None)
    return parsed, dict(tin=u.prompt_tokens, tout=u.completion_tokens,
                        treason=getattr(det, "reasoning_tokens", 0) or 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("needed")
    ap.add_argument("out")
    ap.add_argument("--draws", type=int, default=2)
    ap.add_argument("--draw-ids", default="", help="ask only these draws, e.g. '2' (overrides --draws)")
    ap.add_argument("--sample", type=int, default=0, help="a random N of the decisions (repeat-ask subset)")
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0, help="only the first N decisions (pilot)")
    a = ap.parse_args()

    cfg = resolve_llm_config()
    assert cfg.provider == "openai" and cfg.available, "expected the app's OpenAI config"
    client = _openai_client(cfg.api_key)
    needed = json.load(open(a.needed))
    keys = sorted(needed)
    random.Random(7).shuffle(keys)          # a partial run is a random sample, not one symbol
    if a.sample:
        keys = random.Random(11).sample(keys, min(a.sample, len(keys)))
    if a.limit:
        keys = keys[: a.limit]
    draw_ids = [int(d) for d in a.draw_ids.split(",") if d] or list(range(1, a.draws + 1))

    done = set()
    if os.path.exists(a.out):
        for line in open(a.out):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:    # a line cut short by a stopped run: ask again
                continue
            done.add((r["key"], r["draw"]))
    jobs = [(k, d) for d in draw_ids for k in keys if (k, d) not in done]
    print(f"model {cfg.model}: {len(jobs)} calls to make ({len(done)} already done)", flush=True)

    lock = threading.Lock()
    fh = open(a.out, "a")
    tot = {"n": 0, "tin": 0, "tout": 0, "err": 0, "none": 0}
    t0 = time.time()

    def work(job):
        key, draw = job
        fork, user = needed[key]
        for attempt in range(7):
            try:
                t = time.time()
                parsed, use = ask(client, cfg.model, fork, user)
                rec = dict(key=key, draw=draw, fork=fork, sec=round(time.time() - t, 2), **use)
                if parsed is None:
                    rec.update(category=None, confidence=None, evidence=None)
                else:
                    rec.update(category=parsed.category, confidence=max(0.0, min(1.0, float(parsed.confidence))),
                               evidence=parsed.evidence)
                return rec
            except Exception as exc:  # noqa: BLE001 - rate limits / timeouts: back off and retry
                err = f"{type(exc).__name__}: {exc}"[:300]
                time.sleep(min(60, 2 ** (attempt + 1)) + random.random())
        return dict(key=key, draw=draw, fork=fork, error=err)

    with ThreadPoolExecutor(a.threads) as pool:
        for fut in as_completed([pool.submit(work, j) for j in jobs]):
            rec = fut.result()
            with lock:
                if "error" in rec:
                    tot["err"] += 1
                    print("ERROR", rec["error"], flush=True)
                    continue          # not written: a rerun retries it
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                tot["n"] += 1
                tot["tin"] += rec["tin"]
                tot["tout"] += rec["tout"]
                tot["none"] += rec["category"] is None
                if tot["n"] % 200 == 0 or tot["n"] == len(jobs):
                    el = time.time() - t0
                    cost = tot["tin"] / 1e6 * PRICE_IN + tot["tout"] / 1e6 * PRICE_OUT
                    eta = el / tot["n"] * (len(jobs) - tot["n"])
                    print(f"{tot['n']}/{len(jobs)}  ${cost:.2f}  {tot['n'] / el:.1f}/s  eta {eta / 60:.0f}m  "
                          f"unparsed {tot['none']}  errors {tot['err']}", flush=True)
    cost = tot["tin"] / 1e6 * PRICE_IN + tot["tout"] / 1e6 * PRICE_OUT
    print(f"done: {tot['n']} answers, ${cost:.2f}, {tot['err']} failed after retries", flush=True)


if __name__ == "__main__":
    main()
