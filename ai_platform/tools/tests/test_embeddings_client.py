"""
Unit tests for the Voyage AI embeddings REST client (embeddings_client.py).
Mocks `requests.post` — no network access or real API key needed.
"""

from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_platform.tools import embeddings_client


class IsConfiguredTests(TestCase):
    def test_false_when_no_key_set(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(embeddings_client.is_configured())

    def test_true_when_key_set(self):
        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            self.assertTrue(embeddings_client.is_configured())


class EmbedTextsTests(TestCase):
    def test_raises_without_api_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                embeddings_client.embed_texts(["hello"])

    def test_empty_input_returns_empty_list_without_calling_api(self):
        with patch("ai_platform.tools.embeddings_client.requests.post") as mock_post:
            result = embeddings_client.embed_texts([])
            self.assertEqual(result, [])
            mock_post.assert_not_called()

    def test_returns_embeddings_in_request_order(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"embedding": [0.2, 0.2], "index": 1},
                {"embedding": [0.1, 0.1], "index": 0},
            ]
        }
        mock_response.raise_for_status.return_value = None

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            with patch("ai_platform.tools.embeddings_client.requests.post", return_value=mock_response) as mock_post:
                result = embeddings_client.embed_texts(["a", "b"])

        # Re-sorted by `index` regardless of response ordering.
        self.assertEqual(result, [[0.1, 0.1], [0.2, 0.2]])
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["input"], ["a", "b"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer vk-test")

    def test_input_type_passed_through_when_given(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"embedding": [0.1], "index": 0}]}
        mock_response.raise_for_status.return_value = None

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            with patch("ai_platform.tools.embeddings_client.requests.post", return_value=mock_response) as mock_post:
                embeddings_client.embed_texts(["a"], input_type="query")

        self.assertEqual(mock_post.call_args.kwargs["json"]["input_type"], "query")

    def test_raises_on_http_error(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP 429")

        with patch.dict("os.environ", {"VOYAGE_API_KEY": "vk-test"}):
            with patch("ai_platform.tools.embeddings_client.requests.post", return_value=mock_response):
                with self.assertRaises(Exception):
                    embeddings_client.embed_texts(["a"])
