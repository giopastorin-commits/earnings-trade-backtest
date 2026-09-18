"""Deterministic textual novelty measurement for TRINITY/H2 V0.1.

This module compares exactly two earnings-release texts.  It has no knowledge
of market data, company performance, sentiment, fundamentals, or future
outcomes and performs no data fetching.
"""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
import math
import re
from typing import TypedDict


METHOD = "TFIDF_COSINE_V1"
MIN_WORD_COUNT = 3
_WORD_PATTERN = re.compile(r"\b\w+(?:[.'-]\w+)*\b", re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


class H2InputError(ValueError):
    """Raised when a text cannot support a meaningful H2 comparison."""


class H2NoveltyResult(TypedDict):
    """JSON-serializable result returned by :func:`calculate_h2_novelty`."""

    similarity: float
    h2_novelty: float
    current_word_count: int
    previous_word_count: int
    current_char_count: int
    previous_char_count: int
    method: str


class _TextExtractor(HTMLParser):
    """Collect visible text fragments while discarding basic HTML markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)


def normalize_text(text: str) -> str:
    """Return a deterministic, minimally normalized representation of ``text``.

    Normalization removes basic HTML tags, decodes HTML character references,
    converts text to lowercase, and collapses all whitespace runs to one space.
    Numbers and financial vocabulary are retained.  No stemming, sentiment
    processing, section removal, or external knowledge is applied.

    Raises:
        H2InputError: If ``text`` is not a string or has no textual content.
    """

    if not isinstance(text, str):
        raise H2InputError("text must be a string")

    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except (UnicodeError, ValueError) as error:
        raise H2InputError("text contains invalid HTML markup") from error

    normalized = _WHITESPACE_PATTERN.sub(" ", " ".join(parser.fragments)).strip().lower()
    if not normalized:
        raise H2InputError("text must contain non-whitespace content")
    return normalized


def calculate_h2_novelty(
    current_text: str,
    previous_text: str,
) -> H2NoveltyResult:
    """Measure textual novelty between current and previous earnings releases.

    A TF-IDF vector is built for each normalized document using the vocabulary
    of the two-document corpus.  Term frequency is the raw token count and IDF
    uses ``log((1 + n_documents) / (1 + document_frequency)) + 1``.  The result
    is L2-normalized cosine similarity, with ``h2_novelty = 1 - similarity``.

    Both documents must contain at least :data:`MIN_WORD_COUNT` tokens after
    normalization.  This prevents a numerical score from being presented for a
    comparison too short to be meaningful.

    Raises:
        H2InputError: If either input is invalid, empty, or too short.
    """

    current_normalized = normalize_text(current_text)
    previous_normalized = normalize_text(previous_text)
    current_tokens = _tokenize(current_normalized)
    previous_tokens = _tokenize(previous_normalized)

    _validate_document_length(current_tokens, "current_text")
    _validate_document_length(previous_tokens, "previous_text")

    current_vector, previous_vector = _tfidf_vectors(
        current_tokens, previous_tokens
    )
    similarity = _cosine_similarity(current_vector, previous_vector)
    similarity = min(1.0, max(0.0, similarity))
    novelty = min(1.0, max(0.0, 1.0 - similarity))

    return {
        "similarity": similarity,
        "h2_novelty": novelty,
        "current_word_count": len(current_tokens),
        "previous_word_count": len(previous_tokens),
        "current_char_count": len(current_normalized),
        "previous_char_count": len(previous_normalized),
        "method": METHOD,
    }


def _tokenize(text: str) -> list[str]:
    return _WORD_PATTERN.findall(text)


def _validate_document_length(tokens: list[str], input_name: str) -> None:
    if len(tokens) < MIN_WORD_COUNT:
        raise H2InputError(
            f"{input_name} is too short: at least {MIN_WORD_COUNT} words are required"
        )


def _tfidf_vectors(
    current_tokens: list[str],
    previous_tokens: list[str],
) -> tuple[dict[str, float], dict[str, float]]:
    current_counts = Counter(current_tokens)
    previous_counts = Counter(previous_tokens)
    vocabulary = sorted(current_counts.keys() | previous_counts.keys())

    current_vector: dict[str, float] = {}
    previous_vector: dict[str, float] = {}
    for term in vocabulary:
        document_frequency = int(term in current_counts) + int(term in previous_counts)
        inverse_document_frequency = math.log(3.0 / (1.0 + document_frequency)) + 1.0
        current_vector[term] = current_counts[term] * inverse_document_frequency
        previous_vector[term] = previous_counts[term] * inverse_document_frequency
    return current_vector, previous_vector


def _cosine_similarity(
    current_vector: dict[str, float],
    previous_vector: dict[str, float],
) -> float:
    terms = current_vector.keys()
    dot_product = sum(
        current_vector[term] * previous_vector[term] for term in terms
    )
    current_norm = math.sqrt(sum(value * value for value in current_vector.values()))
    previous_norm = math.sqrt(sum(value * value for value in previous_vector.values()))
    if current_norm == 0.0 or previous_norm == 0.0:
        raise H2InputError("normalized text does not contain measurable terms")
    return dot_product / (current_norm * previous_norm)
