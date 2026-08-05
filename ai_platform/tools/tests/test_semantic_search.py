"""
Unit tests for the shared embeddings-first/TF-IDF-fallback ranking logic
(semantic_search.py). Pure logic + mocked embeddings client — no
network/API key needed.
"""

from unittest import TestCase
from unittest.mock import patch

from ai_platform.tools.semantic_search import rank_by_similarity


class TfidfFallbackTests(TestCase):
    """No VOYAGE_API_KEY set anywhere in these tests, so every call here
    exercises the TF-IDF fallback path — same behavior the original
    incident_search.py had standalone."""

    def test_ranks_more_similar_document_first(self):
        with patch.dict("os.environ", {}, clear=True):
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
        with patch.dict("os.environ", {}, clear=True):
            documents = [f"checkout error number {i}" for i in range(5)]
            ranked = rank_by_similarity("checkout error", documents, top_k=2)

        self.assertLessEqual(len(ranked), 2)

    def test_min_similarity_filters_weak_matches(self):
        with patch.dict("os.environ", {}, clear=True):
            ranked = rank_by_similarity(
                "checkout deployment error", ["checkout deployment error"], min_similarity=1.01
            )

        self.assertEqual(ranked, [])


class EmbeddingsPathTests(TestCase):
    def test_uses_embeddings_when_configured_and_available(self):
        documents = ["doc about pods", "doc about latency"]

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            with patch("ai_platform.tools.semantic_search.embeddings_client.embed_texts") as mock_embed:
                # First call embeds the documents, second embeds the query.
                mock_embed.side_effect = [
                    [[1.0, 0.0], [0.0, 1.0]],
                    [[1.0, 0.0]],
                ]
                ranked = rank_by_similarity("query about pods", documents)

        self.assertEqual(ranked[0].index, 0)
        self.assertGreater(ranked[0].score, ranked[1].score if len(ranked) > 1 else -1)
        mock_embed.assert_any_call(documents, input_type="document")
        mock_embed.assert_any_call(["query about pods"], input_type="query")

    def test_falls_back_to_tfidf_when_embeddings_call_fails(self):
        documents = ["product-catalog scaled to 0 replicas", "checkout slow payment call"]

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            with patch(
                "ai_platform.tools.semantic_search.embeddings_client.embed_texts",
                side_effect=RuntimeError("network down"),
            ):
                ranked = rank_by_similarity("product-catalog scaled to zero", documents)

        # Still gets a sensible ranking via TF-IDF, despite the embeddings
        # call raising.
        self.assertGreaterEqual(len(ranked), 1)
        self.assertEqual(ranked[0].index, 0)

    def test_not_configured_skips_embeddings_call_entirely(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("ai_platform.tools.semantic_search.embeddings_client.embed_texts") as mock_embed:
                rank_by_similarity("anything", ["doc one", "doc two"])

        mock_embed.assert_not_called()
