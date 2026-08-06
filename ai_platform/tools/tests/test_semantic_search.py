"""
Unit tests for the shared TF-IDF ranking logic (semantic_search.py). Pure
logic — no network/API key needed.
"""

from unittest import TestCase

from ai_platform.tools.semantic_search import rank_by_similarity


class TfidfRankingTests(TestCase):
    def test_ranks_more_similar_document_first(self):
        documents = [
            "product-catalog deployment was scaled to 0 replicas",
            "checkout p95 latency spiked due to a slow downstream payment call",
        ]
        ranked = rank_by_similarity("product-catalog scaled to zero replicas", documents)

        self.assertGreaterEqual(len(ranked), 1)
        self.assertEqual(ranked[0].index, 0)

    def test_empty_query_returns_empty(self):
        self.assertEqual(rank_by_similarity("", ["some document"]), [])
        self.assertEqual(rank_by_similarity("   ", ["some document"]), [])

    def test_empty_documents_returns_empty(self):
        self.assertEqual(rank_by_similarity("anything", []), [])

    def test_top_k_caps_results(self):
        documents = [f"checkout error number {i}" for i in range(5)]
        ranked = rank_by_similarity("checkout error", documents, top_k=2)

        self.assertLessEqual(len(ranked), 2)

    def test_min_similarity_filters_weak_matches(self):
        ranked = rank_by_similarity(
            "checkout deployment error", ["checkout deployment error"], min_similarity=1.01
        )

        self.assertEqual(ranked, [])
