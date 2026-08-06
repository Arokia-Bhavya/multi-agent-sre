"""
Shared "rank documents by similarity to a query" logic, used by both
`incident_search.py`'s `search_similar_incidents` and `knowledge_base.py`'s
`search_knowledge_base`.

Uses TF-IDF + cosine similarity (scikit-learn), not learned embeddings:
keyword/phrase-overlap similarity, not true semantic similarity — it won't
catch a paraphrase with zero shared vocabulary, but it's an honest,
zero-setup match for "similar wording," needing no API key, model
download, or network access.

Splitting this out of `incident_search.py` (where the TF-IDF logic
originally lived alone) means `knowledge_base.py` gets the same ranking
logic for free, instead of reimplementing or duplicating it.
"""

from dataclasses import dataclass
from typing import List, Sequence

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


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


def rank_by_similarity(
    query: str, documents: Sequence[str], top_k: int = 5, min_similarity: float = 0.05
) -> List[RankedMatch]:
    """
    Rank `documents` by TF-IDF cosine similarity to `query`, highest first,
    dropping anything below `min_similarity`, capped at `top_k`.

    Callers get back `RankedMatch.index` values into their original
    `documents` sequence, so they can look up whatever richer object (an
    `IncidentRecord`, a `KnowledgeBaseDoc`) each document string came from.
    """
    if not query.strip() or not documents:
        return []

    ranked = _rank_tfidf(query, documents)

    ranked = [m for m in ranked if m.score >= min_similarity]
    ranked.sort(key=lambda m: m.score, reverse=True)
    return ranked[:top_k]
