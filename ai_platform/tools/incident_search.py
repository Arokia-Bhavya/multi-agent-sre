"""
Search over past incident RCA reports (Milestone 6 "historical incident
search" extension, upgraded by the "true semantic/embedding-based incident
search" backlog item).

Complements `IncidentStore.query_history`'s exact filters (service
substring, time window) with fuzzy "have we seen something like this
before" search — for when the user describes symptoms rather than naming
an exact service or alert.

Ranking is delegated to `semantic_search.rank_by_similarity`: true
semantic similarity (Voyage AI embeddings) when `VOYAGE_API_KEY` is
configured, otherwise TF-IDF keyword/phrase-overlap similarity as a
zero-setup fallback that needs no API key, model download, or network
access. See `semantic_search.py` for why it's split out this way (the same
logic now also backs `knowledge_base.py`'s `search_knowledge_base`).
"""

from dataclasses import dataclass
from typing import List

from ai_platform.tools.incident_store import IncidentRecord
from ai_platform.tools.semantic_search import rank_by_similarity


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
    Rank `records` by similarity of their alert/service/RCA text against
    `query`, highest first, dropping anything below `min_similarity`
    (incidents that only share a stray common word/concept, not a
    meaningful match).

    Re-ranks from `records` fresh on every call rather than maintaining a
    persistent index — simple and correct at the incident volumes this
    platform expects (a local demo cluster), at the cost of being O(n) work
    (plus an embeddings API call, if configured) per search rather than
    O(1).
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
    ranked = rank_by_similarity(query, corpus, top_k=top_k, min_similarity=min_similarity)
    return [
        SimilarIncident(record=indexed[match.index][0], similarity=match.score)
        for match in ranked
    ]
