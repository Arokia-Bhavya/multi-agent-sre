# Evals

LLM-quality evals for the RCA pipeline, runbook generation, and the SRE
Copilot's tool-use — distinct from `ai_platform/*/tests/` (pytest, mocked
LLMs, deterministic, safe for every commit). These make real LLM calls
against fixed scenarios and grade the output. Not wired into `pytest`'s
`testpaths` on purpose: they cost real API calls and grade against
heuristic checks, not exact-match assertions.

## Running

Requires `ANTHROPIC_API_KEY` (or `GROQ_API_KEY` with `LLM_PROVIDER=groq`)
in the environment, same as the rest of the platform.

```bash
uv run python -m ai_platform.evals.run_all          # all three suites
uv run python -m ai_platform.evals.run_rca_evals     # RCA quality only
uv run python -m ai_platform.evals.run_runbook_evals # runbook quality only
uv run python -m ai_platform.evals.run_tool_use_evals # copilot tool-routing only
```

### Avoid sharing a rate-limit budget with interactive usage

By default evals read `LLM_PROVIDER`/`LLM_MODEL` the same way `chat.py`/
`server.py` do, so they draw from the same daily quota (Groq's free tier is
100k tokens/day *per model*). The tool-use suite in particular is
token-heavy: every ReAct turn resends the full system prompt plus all 19
tool schemas, so a handful of fixtures with a couple of turns each adds up
fast, and a few iterations while tuning fixtures can burn through the same
budget your interactive session needs — or vice versa.

Set `EVAL_LLM_PROVIDER`/`EVAL_LLM_MODEL` in `.env` to point evals at a
different model (and therefore a different Groq quota bucket) without
touching what `chat.py`/`server.py` use day to day:

```bash
EVAL_LLM_MODEL=llama-3.1-8b-instant   # separate Groq TPD bucket from the default
# EVAL_LLM_PROVIDER=anthropic         # or a different provider entirely, if you have credits
```

See `eval_env.py` for details. Leave both unset and nothing changes.

Each suite prints a per-fixture pass/fail with the specific check(s) that
failed, then a token/cost summary.

## What's covered

- **`run_rca_evals.py`** — feeds canned alert + correlated-findings evidence
  (`fixtures/rca_cases.py`) into the real RCA prompt and grades the
  resulting `RCAFinding` on whether it names the right root cause, whether
  confidence is honestly "low" on the deliberately thin-evidence fixture,
  and whether `supporting_evidence` bullets are grounded in the input
  rather than invented.
- **`run_runbook_evals.py`** — same fixtures, one step further: grades the
  `RunbookDoc` on whether it includes a concrete kubectl/PromQL command
  when the evidence makes one obvious, and whether a low-confidence RCA
  correctly produces a "diagnose first" runbook rather than jumping to a
  fix.
- **`run_tool_use_evals.py`** — drives a real `SRECopilot.ask(...)` call per
  fixture (`fixtures/tool_use_cases.py`) and inspects the tool-call trace:
  did it route to the right tool, and — for the one fixture that matters
  most — did it avoid calling `remediate_scale_deployment` speculatively
  before gathering evidence (the platform's guardrail blocks this
  structurally regardless, but this catches the model attempting it at
  all).

## Adding a fixture

RCA/runbook: add an `RCACase` to `fixtures/rca_cases.py` — an alert +
`correlated_findings` shaped like `InvestigationGraph._correlate_findings`'s
output, plus the keywords/confidence/command patterns you expect.

Tool-use: add a `ToolUseCase` to `fixtures/tool_use_cases.py` — a question,
canned agent return values, and which tool(s) count as a correct route.

## Extending

`scoring.py` is deliberately rule-based (keyword match, substring
grounding, regex, trace inspection) rather than an LLM-judge, so results are
fast, free, and reproducible. If a fixture needs a subtler judgment call
("is this explanation actually well-reasoned") that no cheap heuristic can
catch, add an LLM-judge path there rather than loosening the rule-based
checks — keep the two failure modes (cheap-but-coarse vs. expensive-but-
flaky) separate rather than blending them into one check.

`cost_tracking.py`'s `PRICES_PER_MILLION_TOKENS` table is approximate and
not auto-synced with real pricing — treat dollar figures as relative
(did this change roughly double cost?), not authoritative.
