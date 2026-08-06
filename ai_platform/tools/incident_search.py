"""
Lightweight search over past incident RCA reports (Milestone 6 "historical
incident search" extension).

Complements `IncidentStore.query_history`'s exact filters (service
substring, time window) with fuzzy "have we seen something like this
before" search — for when the user describes symptoms rather than naming
an exact service or alert.

Uses TF-IDF + cosine similarity (scikit-learn), not learned embeddings:
neither of this project's two LLM providers (Anthropic, Groq) expose an
embeddings API, and a local embedding model (e.g. sentence-transformers)
pulls in a heavy torch dependency for what's meant to stay a lightweight
demo platform. TF-IDF is keyword/phrase-overlap similarity, not true
semantic similarity — it won't catch a paraphrase with zero shared
vocabulary — but it's an honest, zero-setup match for "similar wording to a
past RCA report," needing no API key, model download, or network access.
"""

from dataclasses import dataclass
from typing import List

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ai_platform.tools.incident_store import IncidentRecord


@dataclass
class SimilarIncident:
    record: IncidentRecord
    similarity: float


def _document_for(record: IncidentRecord) -> str:
    """
    Build the searchable text for one incident: alert name and service for
    context, plus its RCA report/outcome text where the actual symptom and
    root-cause wording lives.
    """
    parts = [record.alert_name or "", record.service or "", record.content or ""]
    return " ".join(p for p in parts if p).strip()


def search_similar_incidents(
    records: List[IncidentRecord], query: str, top_k: int = 5, min_similarity: float = 0.05
) -> List[SimilarIncident]:
    """
    Rank `records` by TF-IDF cosine similarity of their alert/service/RCA
    text against `query`, highest first, dropping anything below
    `min_similarity` (incidents that only share a stray common word, not a
    meaningful match).

    Rebuilds the TF-IDF index fresh from `records` on every call rather
    than maintaining a persistent index — simple and correct at the
    incident volumes this platform expects (a local demo cluster), at the
    cost of being O(n) work per search rather than O(1).
    """
    if not query.strip():
        return []

    documents = [_document_for(r) for r in records]
    # Only records with something to actually match against — an incident
    # still "investigating" with no report yet has nothing useful to index.
    indexed = [(r, doc) for r, doc in zip(records, documents) if doc]
    if not indexed:
        return []

    corpus = [doc for _, doc in indexed]
    vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    try:
        corpus_matrix = vectorizer.fit_transform(corpus)
    except ValueError:
        # e.g. every document was entirely stop words/punctuation after
        # tokenization — no usable vocabulary to rank against.
        return []

    query_vector = vectorizer.transform([query])
    similarities = cosine_similarity(query_vector, corpus_matrix)[0]

    ranked = sorted(
        (
            SimilarIncident(record=record, similarity=float(score))
            for (record, _), score in zip(indexed, similarities)
        ),
        key=lambda match: match.similarity,
        reverse=True,
    )
    results = [match for match in ranked if match.similarity >= min_similarity]
    return results[:top_k]
