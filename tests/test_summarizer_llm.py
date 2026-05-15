"""Tests for OllamaSummarizer.

The Ollama HTTP call is monkeypatched out; we exercise prompt building,
response parsing, fallback behavior, and the build_summarizer factory."""

from unittest.mock import patch

import pytest

from localmem.config import (
    ConsolidationConfig,
    LocalmemConfig,
    RetentionConfig,
    StorageConfig,
)
from localmem.models import Entry
from localmem.summarizer import TemplateSummarizer
from localmem.summarizer_llm import OllamaSummarizer, build_summarizer


# --- Model URL parsing ---


class TestModelParsing:
    def test_bare_name(self):
        url, model = OllamaSummarizer._parse_model("qwen2.5:3b", None)
        assert url == "http://localhost:11434"
        assert model == "qwen2.5:3b"

    def test_ollama_scheme(self):
        url, model = OllamaSummarizer._parse_model("ollama://otherhost:9999/qwen2.5:3b", None)
        assert url == "http://otherhost:9999"
        assert model == "qwen2.5:3b"

    def test_explicit_base_overrides(self):
        url, model = OllamaSummarizer._parse_model("model-x", "http://elsewhere:11000")
        assert url == "http://elsewhere:11000"
        assert model == "model-x"


# --- Prompt build ---


class TestPromptBuild:
    def test_includes_group_label_and_count(self):
        s = OllamaSummarizer(model="x")
        entries = [
            Entry(wing="router", room="r", agent_id="a",
                  content=f"entry {i} content", importance=0.1 * i)
            for i in range(1, 4)
        ]
        prompt = s._build_prompt(entries, "router/r 2026-W12")
        assert "router/r 2026-W12" in prompt
        assert "3 entries" in prompt
        # Top entry by importance should appear
        assert "entry 3 content" in prompt

    def test_extracts_terms(self):
        text = """[Summary] x
Top entries by importance:
- (0.50) thing

Frequent terms: routing, latency, anthropic, decision
"""
        terms = OllamaSummarizer._extract_terms(text)
        assert terms == ["routing", "latency", "anthropic", "decision"]

    def test_extracts_terms_missing(self):
        terms = OllamaSummarizer._extract_terms("[Summary] x\nNo terms here.")
        assert terms == []


# --- Successful summarize ---


class TestSummarize:
    def test_uses_llm_response_when_call_succeeds(self):
        s = OllamaSummarizer(model="x")
        entries = [
            Entry(wing="router", room="r", agent_id="a", content="A fact", importance=0.4),
            Entry(wing="router", room="r", agent_id="a", content="B fact", importance=0.6),
        ]
        with patch.object(s, "_call_ollama", return_value=(
            "[Summary] router/r — 2 entries from 2026-04-01 to 2026-04-02\n"
            "Top entries by importance:\n"
            "- (0.60) B fact\n"
            "- (0.40) A fact\n"
            "Key themes: facts; observations\n"
            "Frequent terms: fact, b, a\n"
        )):
            bundle = s.summarize(entries, "router/r")
        assert "router/r" in bundle["text"]
        assert bundle["entry_count"] == 2
        assert bundle["max_importance"] == pytest.approx(0.6)
        assert bundle["top_terms"] == ["fact", "b", "a"]

    def test_falls_back_on_http_error(self):
        s = OllamaSummarizer(model="x")
        entries = [
            Entry(wing="router", room="r", agent_id="a", content="X", importance=0.5),
            Entry(wing="router", room="r", agent_id="a", content="Y", importance=0.5),
        ]
        with patch.object(s, "_call_ollama", side_effect=ConnectionError("refused")):
            bundle = s.summarize(entries, "router/r 2026-W12")
        # Template fallback shape
        assert bundle["entry_count"] == 2
        assert "Top entries by importance" in bundle["text"]
        # Subsequent failures are silent
        with patch.object(s, "_call_ollama", side_effect=ConnectionError("still")):
            bundle2 = s.summarize(entries, "router/r 2026-W12")
        assert bundle2["entry_count"] == 2

    def test_falls_back_on_empty_response(self):
        s = OllamaSummarizer(model="x")
        entries = [
            Entry(wing="router", room="r", agent_id="a", content="X", importance=0.5),
            Entry(wing="router", room="r", agent_id="a", content="Y", importance=0.5),
        ]
        with patch.object(s, "_call_ollama", return_value="   "):
            bundle = s.summarize(entries, "router/r 2026-W12")
        assert "Top entries by importance" in bundle["text"]

    def test_empty_entries_raises(self):
        s = OllamaSummarizer(model="x")
        with pytest.raises(ValueError):
            s.summarize([], "x")


# --- Factory ---


def _cfg(summarizer="template", llm_model=None) -> LocalmemConfig:
    return LocalmemConfig(
        storage=StorageConfig(),
        retention=RetentionConfig(
            consolidation=ConsolidationConfig(
                summarizer=summarizer,
                llm_model=llm_model,
            ),
        ),
    )


class TestFactory:
    def test_default_returns_template(self):
        s = build_summarizer(_cfg())
        assert isinstance(s, TemplateSummarizer)

    def test_llm_with_model_returns_ollama(self):
        s = build_summarizer(_cfg(summarizer="llm", llm_model="qwen2.5:3b"))
        assert isinstance(s, OllamaSummarizer)
        assert s.model == "qwen2.5:3b"

    def test_llm_without_model_falls_back(self):
        s = build_summarizer(_cfg(summarizer="llm", llm_model=None))
        assert isinstance(s, TemplateSummarizer)

    def test_llm_url_form_parsed(self):
        s = build_summarizer(_cfg(
            summarizer="llm", llm_model="ollama://gpu-host:11434/qwen2.5:3b"
        ))
        assert isinstance(s, OllamaSummarizer)
        assert s.base_url == "http://gpu-host:11434"
        assert s.model == "qwen2.5:3b"
