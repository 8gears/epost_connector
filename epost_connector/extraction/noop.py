"""The default extractor: does nothing, on purpose."""

from __future__ import annotations

from typing import Any

from epost_connector.extraction.base import ExtractionResult, LetterExtractor


class NoopExtractor(LetterExtractor):
	"""Extracts nothing, so letters stop at `Downloaded` and wait for a human."""

	name = "None"

	def extract(self, letter_doc: Any, pdf_bytes: bytes) -> ExtractionResult | None:
		return None
