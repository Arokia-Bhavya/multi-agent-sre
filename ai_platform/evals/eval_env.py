"""
Eval-specific LLM environment overrides.

Problem: `run_*.py` builds its LLM the same way `chat.py`/`server.py` do —
by reading `LLM_PROVIDER`/`LLM_MODEL` (and, since the copilot/RCA tiering
split, `COPILOT_LLM_MODEL`) straight from the environment (see
`InvestigationGraph.llm` and `copilot/agent.py::_build_llm`). That means
evals and interactive/production usage share one daily quota bucket: on
Groq's free tier that's 100k tokens/day *per model*, so a few eval
iterations while tuning fixtures can eat the same budget your interactive
chat session needs, and the reverse — chatting with the copilot earlier in
the day can leave nothing for an eval run later.

Fix: set `EVAL_LLM_PROVIDER`/`EVAL_LLM_MODEL`/`EVAL_COPILOT_LLM_MODEL` in
`.env` *in addition to* `LLM_PROVIDER`/`LLM_MODEL`/`COPILOT_LLM_MODEL` —
evals then point at different models (and, on Groq, therefore different
per-model quota buckets) without touching what `chat.py`/`server.py` use
day to day. Leave them unset and evals behave exactly as before, reading
`LLM_PROVIDER`/`LLM_MODEL`/`COPILOT_LLM_MODEL` like everything else.

**Tier-aware, matching the production split**: `run_rca_evals.py` and
`run_runbook_evals.py` exercise `InvestigationGraph`'s RCA/runbook
structured calls, which production always runs on the *strong* tier
(`LLM_PROVIDER`/`LLM_MODEL`) regardless of what the copilot's own
conversational loop uses — so they call `apply_eval_llm_overrides(tier=
"strong")`, which only ever touches `LLM_PROVIDER`/`LLM_MODEL`.
`run_tool_use_evals.py` drives `SRECopilot`'s own model, which production
runs on the *cheap* tier (`COPILOT_LLM_MODEL`, same provider as
`LLM_PROVIDER` — see `agent.py`'s `DEFAULT_CHEAP_MODELS`) — so it calls
`apply_eval_llm_overrides(tier="cheap")`, which touches `LLM_PROVIDER` (the
cheap tier always shares the strong tier's provider) and
`COPILOT_LLM_MODEL`, but deliberately never `LLM_MODEL`.

Setting a single blanket `EVAL_LLM_MODEL` for everything (the pre-tier-aware
behavior) silently made the RCA/runbook eval test the *cheap* model instead
of the strong tier it's meant to validate — an eval-configuration mismatch
that looked like an RCA quality regression but wasn't representative of what
production runs for that path at all.

A tool-use fixture that happens to exercise `investigate_service`/
`generate_runbook` still calls the real strong tier for its RCA/runbook step
either way — that's not an eval bug, it's the same cross-tier behavior a
real "why is X slow" chat message triggers in production, so both tiers'
quotas matter for a clean tool-use run.

Example `.env` entries to add:

    # Keep interactive usage on the defaults...
    LLM_PROVIDER=groq
    LLM_MODEL=llama-3.3-70b-versatile
    COPILOT_LLM_MODEL=llama-3.1-8b-instant
    # ...but point RCA/runbook evals at a different strong-tier quota bucket:
    EVAL_LLM_MODEL=llama-3.3-70b-versatile
    # ...and tool-use evals at a different cheap-tier quota bucket:
    EVAL_COPILOT_LLM_MODEL=llama-3.1-8b-instant
    # EVAL_LLM_PROVIDER=anthropic   # applies to both tiers, if set

Must be called *after* `load_dotenv()` and *before* anything reads
`LLM_PROVIDER`/`LLM_MODEL`/`COPILOT_LLM_MODEL` (i.e. before constructing an
`InvestigationGraph` or `SRECopilot`) — every `run_*.py` entrypoint does
both at import time.
"""

import os
from typing import Literal


def apply_eval_llm_overrides(tier: Literal["strong", "cheap"] = "strong") -> None:
    eval_provider = os.getenv("EVAL_LLM_PROVIDER")
    if eval_provider:
        os.environ["LLM_PROVIDER"] = eval_provider

    if tier == "strong":
        eval_model = os.getenv("EVAL_LLM_MODEL")
        if eval_model:
            os.environ["LLM_MODEL"] = eval_model
    elif tier == "cheap":
        eval_copilot_model = os.getenv("EVAL_COPILOT_LLM_MODEL")
        if eval_copilot_model:
            os.environ["COPILOT_LLM_MODEL"] = eval_copilot_model
    else:
        raise ValueError(f"Unknown tier {tier!r}; expected 'strong' or 'cheap'")
