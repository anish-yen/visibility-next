"""Agentic loop as a LangGraph state graph, beside the legacy loop_runner.

Opt in with ZETEUM_LOOP_ENGINE=graph; the default stays "legacy".

Nothing is imported eagerly here: `app.graph.loop_graph` pulls in langgraph,
and the rest of the backend must keep importing fine when that dependency is
absent (an older deploy, say). Import the submodule you need directly.
"""
