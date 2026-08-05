"""
LLM-quality evals for the Multi-Agent SRE Platform.

Distinct from `ai_platform/*/tests/` (pytest, deterministic, mocked LLMs,
runs on every commit): everything under here makes real LLM calls against
fixed scenarios and grades the output on three axes:

- Tool-use / function eval (`run_tool_use_evals.py`): does the copilot pick
  the right tool, with the right args, in the right order (e.g. gathering
  evidence before proposing a remediation)? Checkable programmatically off
  the LangGraph tool-call trace (`CopilotReply.steps`) — pass/fail, no
  judgment call.
- Output/quality eval (`run_rca_evals.py`, `run_runbook_evals.py`): is the
  RCA's root cause actually right, is confidence honestly low on thin
  evidence, is every claimed piece of supporting evidence grounded in the
  input (not hallucinated), does the runbook include a real command when
  the evidence supports one. Scored with fixture-based checks
  (`scoring.py`), not exact string match.
- Token/cost tracking (`cost_tracking.py`): tokens and estimated $ per LLM
  call, logged alongside every eval run so a prompt or model change that
  quietly balloons cost per turn shows up in the same report as
  correctness — a performance/regression signal, not a pass/fail gate.

Requires a real ANTHROPIC_API_KEY (or GROQ_API_KEY with LLM_PROVIDER=groq)
in the environment and costs real API calls — not part of `pytest`/CI's
`testpaths`. Run manually via `run_all.py`, or on a schedule.
"""
