"""
Shared "rank documents by similarity to a query" logic (Milestone 6 "true
semantic/embedding-based incident search" extension), used by both
`incident_search.py`'s `search_similar_incidents` and the new
`knowledge_base.py`'s `search_knowledge_base`.

Two strategies, tried in order:

1. Embeddings (Voyage AI, via `embeddings_client.py`): true semantic
   similarity — catches a paraphrase with zero shared vocabulary (e.g. "the
   checkout page won't load" matching a doc about "frontend returning 5xx
   errors"). Used whenever `VOYAGE_API_KEY` is configured.
2. TF-IDF + cosine similarity (scikit-learn): keyword/phrase-overlap
   similarity. Zero-setup fallback — no API key, model download, or network
   access needed — used when no embeddings key is configured, or if the
   embeddings call fails for any reason (network, rate limit, bad key).
   This is the platform's existing "degrade gracefully rather than break"
   pattern (every agent/client already catches its own failures), extended
   to search.

Splitting this out of `incident_search.py` (where the TF-IDF logic
originally lived alone) means `knowledge_base.py` gets the same
embeddings-first/TF-IDF-fallback behavior for free, instead of
reimplementing or duplicating it.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ai_platform.tools import embeddings_client


@dataclass
class RankedMatch:
    """One document's rank: its position in the `documents` sequence
    passed to `rank_by_similarity`, and its similarity score."""

    index: int
    score: float


def _rank_tfidf(query: str, documents: Sequence[str]) -> List[RankedMatch]:
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    try:
        corpus_matrix = vectorizer.fit_transform(documents)
    except ValueError:
        # e.g. every document was entirely stop words/punctuation after
        # tokenization — no usable vocabulary to rank against.
        return []
    query_vector = vectorizer.transform([query])
    similarities = cosine_similarity(query_vector, corpus_matrix)[0]
    return [RankedMatch(index=i, score=float(score)) for i, score in enumerate(similarities)]


def _rank_embeddings(query: str, documents: Sequence[str]) -> Optional[List[RankedMatch]]:
    """
    Returns None (never an empty list) when embeddings aren't usable —
    unconfigured, or the API call failed — so the caller can tell "fall
    back to TF-IDF" apart from "the embeddings call genuinely ran and
    found zero similarity anywhere," which is a real possible outcome and
    would otherwise be indistinguishable from "not configured."
    """
    if not embeddings_client.is_configured():
        return None
    try:
        doc_vectors = embeddings_client.embed_texts(list(documents), input_type="document")
        query_vector = embeddings_client.embed_texts([query], input_type="query")[0]
    except Exception:  # noqa: BLE001 - any failure here just means "fall back to TF-IDF"
        return None

    import numpy as np  # local import: only needed on the embeddings path

    doc_matrix = np.array(doc_vectors)
    query_matrix = np.array(query_vector).reshape(1, -1)
    similarities = cosine_similarity(query_matrix, doc_matrix)[0]
    return [RankedMatch(index=i, score=float(score)) for i, score in enumerate(similarities)]


def rank_by_similarity(
    query: str, documents: Sequence[str], top_k: int = 5, min_similarity: float = 0.05
) -> List[RankedMatch]:
    """
    Rank `documents` by similarity to `query`, highest first, dropping
    anything below `min_similarity`, capped at `top_k`.

    Tries Voyage embeddings first (if configured and reachable), falling
    back to TF-IDF otherwise. Callers get back `RankedMatch.index` values
    into their original `documents` sequence, so they can look up whatever
    richer object (an `IncidentRecord`, a `KnowledgeBaseDoc`) each document
    string came from.
    """
    if not query.strip() or not documents:
        return []

    ranked = _rank_embeddings(query, documents)
    if ranked is None:
        ranked = _rank_tfidf(query, documents)

    ranked = [m for m in ranked if m.score >= min_similarity]
    ranked.sort(key=lambda m: m.score, reverse=True)
    return ranked[:top_k]
