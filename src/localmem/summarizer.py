"""LOCALMEM summarizers — produce a single text + metadata bundle for a group
of entries. The template summarizer is deterministic and dependency-free; an
LLM-backed summarizer is reserved for v0.4.0 and will share this interface."""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from .models import Entry


# Common English stopwords. Trimmed list — we only need to suppress noise in
# the "frequent terms" line, not produce a real linguistic summary.
_STOPWORDS = frozenset(
    """a an the and or but if then so because of for to from in on at by with
    is are was were be been being have has had do does did this that these those
    it its as i you he she we they me him her us them my your his our their
    not no yes can could should would will may might must shall about into out
    up down over under again more most some any all each every other another""".split()
)

_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _tokens(text: str) -> Iterable[str]:
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in _STOPWORDS:
            continue
        yield tok


class TemplateSummarizer:
    """Deterministic template-based summarizer.

    Produces a single string that captures the gist of a group of entries:
      - top-N entries by importance (preview-truncated)
      - frequent term list
      - date range and source count
      - source ids (for traceability when reading the consolidated entry)
    """

    def __init__(self, top_n: int = 5, top_terms: int = 10, preview_chars: int = 140):
        self.top_n = top_n
        self.top_terms = top_terms
        self.preview_chars = preview_chars

    def summarize(self, entries: list[Entry], group_label: str) -> dict:
        if not entries:
            raise ValueError("cannot summarize empty group")

        sorted_by_imp = sorted(
            entries, key=lambda e: e.importance, reverse=True
        )
        top = sorted_by_imp[: self.top_n]

        terms = Counter()
        for e in entries:
            terms.update(_tokens(e.content or ""))
        top_terms = [t for t, _ in terms.most_common(self.top_terms)]

        timestamps = sorted(e.created_at for e in entries if e.created_at)
        date_range = (
            (timestamps[0], timestamps[-1]) if timestamps else ("", "")
        )

        lines: list[str] = []
        lines.append(
            f"[Summary] {group_label} — {len(entries)} entries"
            f" from {date_range[0]} to {date_range[1]}"
        )
        lines.append("")
        lines.append("Top entries by importance:")
        for e in top:
            preview = (e.summary or e.content or "").strip().replace("\n", " ")
            if len(preview) > self.preview_chars:
                preview = preview[: self.preview_chars - 1] + "…"
            lines.append(f"- ({e.importance:.2f}) {preview}")

        if top_terms:
            lines.append("")
            lines.append(f"Frequent terms: {', '.join(top_terms)}")

        text = "\n".join(lines)
        return {
            "text": text,
            "top_terms": top_terms,
            "date_range": list(date_range),
            "entry_count": len(entries),
            "max_importance": max(e.importance for e in entries),
            "avg_importance": sum(e.importance for e in entries) / len(entries),
        }
