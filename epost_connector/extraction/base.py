"""The contract every letter extractor implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class ExtractionResult:
	"""What an extractor claims to have read out of a letter PDF.

	Every field is optional: an extractor that recognises an invoice number but
	no amount returns exactly that, and the human fills in the rest. `confidence`
	is the extractor's own 0..1 estimate and is not interpreted here.
	"""

	vendor_name: str | None = None
	invoice_number: str | None = None
	invoice_date: date | str | None = None
	due_date: date | str | None = None
	currency: str | None = None
	net_amount: float | None = None
	vat_amount: float | None = None
	gross_amount: float | None = None
	iban: str | None = None
	qr_reference: str | None = None
	summary: str | None = None
	confidence: float = 0.0
	raw: dict[str, Any] = field(default_factory=dict)


class LetterExtractor(ABC):
	"""Turns a letter PDF into an `ExtractionResult`.

	Implementations must be side-effect free: read the bytes, return a result.
	Persisting it is the pipeline's job. Return `None` when nothing usable was
	found — that is not an error and must not raise.
	"""

	name: str = "base"

	@abstractmethod
	def extract(self, letter_doc: Any, pdf_bytes: bytes) -> ExtractionResult | None:
		"""Extract invoice-shaped data from `pdf_bytes`, or return None."""
		raise NotImplementedError
