"""
Unit tests for the knowledge base loader/search (knowledge_base.py).
Uses a temp directory of fake markdown files — no dependency on the real
knowledge_base/ content or a network/API key.
"""

import os
import tempfile
from unittest import TestCase
from unittest.mock import patch

from ai_platform.tools.knowledge_base import load_knowledge_base, search_knowledge_base


def _write(dir_path: str, filename: str, content: str) -> None:
    with open(os.path.join(dir_path, filename), "w") as f:
        f.write(content)


class LoadKnowledgeBaseTests(TestCase):
    def test_loads_markdown_files_with_title_from_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "a.md", "# Pod Down\n\nSome content about pods.")
            _write(tmp, "b.md", "# High Latency\n\nSome content about latency.")

            docs = load_knowledge_base(tmp)

        titles = sorted(doc.title for doc in docs)
        self.assertEqual(titles, ["High Latency", "Pod Down"])

    def test_readme_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "README.md", "# Format guide\n\nHow to add playbooks.")
            _write(tmp, "real-doc.md", "# Real Doc\n\nActual content.")

            docs = load_knowledge_base(tmp)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "Real Doc")

    def test_missing_title_falls_back_to_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "untitled.md", "just some text, no heading")

            docs = load_knowledge_base(tmp)

        self.assertEqual(docs[0].title, "untitled.md")

    def test_empty_file_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "empty.md", "   \n\n  ")

            docs = load_knowledge_base(tmp)

        self.assertEqual(docs, [])

    def test_missing_directory_returns_empty_list(self):
        self.assertEqual(load_knowledge_base("/nonexistent/path/for/sure"), [])


class SearchKnowledgeBaseTests(TestCase):
    def test_ranks_more_relevant_doc_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                tmp, "pod-down.md",
                "# Pod Down\n\nPod stuck in CrashLoopBackOff, container not reporting metrics, "
                "deployment scaled to 0 replicas.",
            )
            _write(
                tmp, "high-latency.md",
                "# High Latency\n\nSlow p95 latency, downstream dependency call taking too long.",
            )

            with patch.dict("os.environ", {}, clear=True):  # force TF-IDF path
                results = search_knowledge_base("pod crash loop scaled to zero", kb_dir=tmp)

        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0].doc.title, "Pod Down")

    def test_empty_query_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "a.md", "# A\n\nsome content")
            self.assertEqual(search_knowledge_base("", kb_dir=tmp), [])

    def test_no_docs_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(search_knowledge_base("anything", kb_dir=tmp), [])

    def test_real_starter_playbooks_load_and_are_searchable(self):
        """Smoke test against the actual knowledge_base/ directory shipped
        with the repo (DEFAULT_KB_DIR) — confirms the starter content is
        well-formed, not just the loader logic in isolation."""
        with patch.dict("os.environ", {}, clear=True):  # force TF-IDF path
            docs = load_knowledge_base()
            results = search_knowledge_base("pod crash loop, no metrics reporting")

        self.assertGreaterEqual(len(docs), 4)
        self.assertGreaterEqual(len(results), 1)
