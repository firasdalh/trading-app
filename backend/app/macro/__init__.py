"""Macro / news / positioning intelligence layer.

A CONTEXT and RISK layer, not a strategy. Nothing in this package may produce a direction: it can
say "wait", "this is riskier than it looks", or "here is what the crowd is doing", and that is all.
The deterministic engine in app/agents/orchestrator.py remains the only source of long/short.

See app/macro/engine.py for the news-risk score and the lock windows, and app/macro/positioning.py
for the crowding model.
"""
