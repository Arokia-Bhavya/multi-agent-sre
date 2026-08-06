"""
Token/cost tracking for eval runs.

A `BaseCallbackHandler` that reads token usage off `on_llm_end`, which fires
regardless of whatever's wrapped around the underlying chat model call
(including `with_structured_output`, which otherwise loses the raw
`AIMessage.usage_metadata` once it parses the result into a pydantic
object) — so this is the one place usage is reliably observable for both
plain `.invoke()` calls (agent.py's ReAct loop) and structured-output calls
(rca.py, runbook.py).

Prices below are approximate list prices per million tokens and are NOT
kept in sync automatically — check current pricing before trusting the
dollar figures for anything beyond rough relative comparisons (e.g. "did
this prompt change roughly double cost per call").
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from langchain_core.callbacks import BaseCallbackHandler

# $ per million tokens (input, output). Approximate — update as pricing changes.
PRICES_PER_MILLION_TOKENS: Dict[str, Dict[str, float]] = {
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-opus-5": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
}


def _price_for(model: Optional[str]) -> Dict[str, float]:
    if not model:
        return {"input": 0.0, "output": 0.0}
    for key, price in PRICES_PER_MILLION_TOKENS.items():
        if key in model:
            return price
    return {"input": 0.0, "output": 0.0}


@dataclass(eq=False)
class TokenCostTracker(BaseCallbackHandler):
    """
    Pass an instance of this as a LangChain callback
    (`llm.invoke(messages, config={"callbacks": [tracker]})`) to accumulate
    token usage and estimated cost across one or more LLM calls. Reusable
    across an eval run: call `.reset()` between fixtures if you want
    per-fixture numbers instead of a running total.
    """

    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    # Keep pydantic/dataclass machinery from choking on the callback base class.
    run_inline: bool = field(default=True)

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.calls += 1
        usage = None
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("usage") or llm_output.get("token_usage")
        if usage:
            self.input_tokens += usage.get("input_tokens") or usage.get("prompt_tokens") or 0
            self.output_tokens += usage.get("output_tokens") or usage.get("completion_tokens") or 0
            return

        # Fall back to per-message usage_metadata (set by langchain-anthropic/
        # langchain-groq on the AIMessage itself even when llm_output is empty).
        for gen_list in getattr(response, "generations", []) or []:
            for gen in gen_list:
                message = getattr(gen, "message", None)
                meta = getattr(message, "usage_metadata", None) if message else None
                if meta:
                    self.input_tokens += meta.get("input_tokens", 0) or 0
                    self.output_tokens += meta.get("output_tokens", 0) or 0

    @property
    def estimated_cost_usd(self) -> float:
        price = _price_for(self.model)
        return (
            self.input_tokens / 1_000_000 * price["input"]
            + self.output_tokens / 1_000_000 * price["output"]
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 5),
        }
