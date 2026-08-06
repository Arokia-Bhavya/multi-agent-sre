"""
Static knowledge base search over human-authored markdown runbooks/
playbooks (Milestone 6 "knowledge base integration" extension).

Distinct from `incident_search.py`'s `search_similar_incidents`: that
searches this platform's own auto-generated incident history (what
happened and what the LLM concluded last time this exact alert fired).
This searches reference material that exists independent of any specific
incident ever having fired — established playbooks for known failure
patterns, written once and reused every time, the way a team's runbook
wiki works. A brand-new incident with no history at all can still match a
knowledge base doc; it can never match an incident-history search.

Documents live as markdown files directly under `knowledge_base/` at the
repo root (see `knowledge_base/README.md` for the expected format). Each
file is indexed as a single document rather than chunked by section —
these playbooks are short enough (a few hundred words) that splitting
would lose more context (a diagnostic step separated from the symptom
description above it) than it gains, and the doc volumes this project
expects (a handful of playbooks, not a production wiki) don't need
finer-grained retrieval.

Ranking is delegated to `semantic_search.rank_by_similarity` — the same
TF-IDF keyword/phrase-overlap logic `search_similar_incidents` uses, so a
query needs some actual shared wording with a playbook (e.g. "502", "error
rate") to match; it won't catch a pure paraphrase with no shared vocabulary.
"""

import glob
import os
from dataclasses import dataclass
from typing import List

from ai_platform.tools.semantic_search import rank_by_similarity

# Resolves relative to this file (ai_platform/tools/) rather than cwd, so it
# works the same regardless of whether the process is launched from the
# repo root, ai_platform/copilot, or ai_platform/webui — same pattern
# incident_store.py's DEFAULT_DB_PATH uses.
DEFAULT_KB_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "knowledge_base")
)


@dataclass
class KnowledgeBaseDoc:
    path: str
    title: str
    content: str


@dataclass
class KnowledgeBaseMatch:
    doc: KnowledgeBaseDoc
    similarity: float


def _title_from(content: str, path: str) -> str:
    """First top-level markdown heading (`# Title`) in the file, or its
    filename if it has none."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return os.path.basename(path)


def load_knowledge_base(kb_dir: str = DEFAULT_KB_DIR) -> List[KnowledgeBaseDoc]:
    """
    Load every `*.md` file directly under `kb_dir` (non-recursive) as one
    document each. `README.md` is excluded by name — it's a format guide
    for humans adding new playbooks, not indexable content. Missing
    directory or no matching files returns an empty list rather than
    raising, so `search_knowledge_base` degrades to "no results" instead of
    crashing when nothing's been added yet.
    """
    if not os.path.isdir(kb_dir):
        return []

    docs = []
    for path in sorted(glob.glob(os.path.join(kb_dir, "*.md"))):
        if os.path.basename(path).lower() == "readme.md":
            continue
        with open(path, "r") as f:
            content = f.read()
        if not content.strip():
            continue
        docs.append(KnowledgeBaseDoc(path=path, title=_title_from(content, path), content=content))
    return docs


def search_knowledge_base(
    query: str, top_k: int = 5, min_similarity: float = 0.05, kb_dir: str = DEFAULT_KB_DIR
) -> List[KnowledgeBaseMatch]:
    """
    Rank knowledge base docs by similarity to `query`, highest first,
    dropping anything below `min_similarity`.

    Reloads and re-ranks from disk on every call rather than maintaining a
    persistent index — same simplicity-over-throughput tradeoff
    `search_similar_incidents` makes, appropriate at the doc volumes a
    project like this has.
    """
    if not query.strip():
        return []

    docs = load_knowledge_base(kb_dir)
    if not docs:
        return []

    corpus = [doc.content for doc in docs]
    ranked = rank_by_similarity(query, corpus, top_k=top_k, min_similarity=min_similarity)
    return [KnowledgeBaseMatch(doc=docs[match.index], similarity=match.score) for match in ranked]
