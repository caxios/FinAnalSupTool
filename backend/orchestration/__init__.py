"""
orchestration
─────────────
The Agent Orchestration Layer: a LangGraph state graph that decomposes an
ad-hoc research question into sub-tasks, dispatches them across the
deterministic (structured_db SQL) and probabilistic (hybrid_search) tools in
parallel, and hands the results to Phase 6's context assembly + synthesis.

This is a NEW graph, not a rewrite of the MAS pipeline (services/pipeline.py
and agents/*_agent.py are untouched) — it powers the interactive "ask a
question about data already fetched" surface (POST /analysis/query-data),
not the fixed-rubric field agents. See implementation_plan/
new_db_architecture_plan/Phase0_Overview_and_Roadmap.md for the full
scope boundary.

  - ``state``    — the ResearchState / SubTask shapes threaded through the graph
  - ``planner``  — decomposes a question into sub_tasks (one Gemini call)
  - ``tools``    — thin adapters: sub_task -> structured_db / hybrid_search
  - ``graph``    — wires plan -> {sql, footnote, search} -> assemble -> synthesize
"""
