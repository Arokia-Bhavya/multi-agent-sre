"""
Thin REST client for Voyage AI embeddings (Milestone 6 "true
semantic/embedding-based incident search" + "knowledge base integration"
extensions).

Neither of this project's two LLM providers (Anthropic, Groq) expose an
embeddings API — see the original design note this module replaces in
`incident_search.py`'s docstring history. Voyage AI is Anthropic's
recommended embeddings partner, has a free tier, and its REST API is a
single POST — calling it directly with `requests` (already a dependency)
avoids pulling in the `voyageai` SDK for one endpoint.

Optional everywhere it's used: with no `VOYAGE_API_KEY` set,
`is_configured()` is False and every call site (`semantic_search.py`)
falls back to its existing TF-IDF/keyword behavior. Nothing in this
project *requires* an embeddings key to run — this only upgrades keyword
overlap to true semantic similarity when one is present.
"""

import os
from typing import List, Optional

import requests

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"

# voyage-3.5-lite: cheapest/fastest of Voyage's general-purpose models,
# plenty for this project's document volumes (a handful of incidents/
# playbooks, not a production-scale corpus). Overridable via env in case a
# larger deployment wants voyage-3.5 or a domain-specific model instead.
DEFAULT_MODEL = os.getenv("VOYAGE_EMBED_MODEL", "voyage-3.5-lite")


def is_configured() -> bool:
    """Whether a Voyage API key is available. Callers should check this
    (or catch the RuntimeError embed_texts raises without one) before
    relying on embeddings-backed search."""
    return bool(os.getenv("VOYAGE_API_KEY"))


def embed_texts(
    texts: List[str], input_type: Optional[str] = None, timeout: float = 15
) -> List[List[float]]:
    """
    Embed a batch of texts via Voyage AI, returned in the same order as
    `texts`.

    Args:
        texts: Strings to embed. Returns [] immediately for an empty list
            (no API call, no key required in that case).
        input_type: Voyage's optional "query" vs. "document" hint, which
            biases the embedding for asymmetric search (a short question
            against a longer document) per
            https://docs.voyageai.com/docs/embeddings. Omit for symmetric
            use.
        timeout: Request timeout in seconds.

    Raises:
        RuntimeError: VOYAGE_API_KEY is not set.
        requests.RequestException: network failure.
        requests.HTTPError: non-2xx response (bad key, rate limit, etc.).

    Callers that want graceful degradation rather than a raised exception
    should go through `semantic_search.rank_by_similarity`, which catches
    all of the above and falls back to TF-IDF.
    """
    if not texts:
        return []

    api_key = os.getenv("VOYAGE_API_KEY")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY is not set")

    payload = {"model": DEFAULT_MODEL, "input": texts}
    if input_type:
        payload["input_type"] = input_type

    response = requests.post(
        VOYAGE_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()

    # Voyage documents `data` as returned in request order, but sort
    # explicitly on the `index` field rather than trust that — a cheap
    # safeguard against a response-ordering assumption silently breaking.
    ordered = sorted(data["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in ordered]
