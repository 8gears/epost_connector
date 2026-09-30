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
	#: One of `DOCUMENT_KINDS`, or None when the extractor does not classify.
	document_kind: str | None = None
	vendor_tax_id: str | None = None
	#: The vendor's postal address as printed, one line per address line.
	vendor_address: str | None = None
	service_period_from: date | str | None = None
	service_period_to: date | str | None = None
	#: An ERPNext Country name, e.g. "Switzerland".
	vendor_country: str | None = None
	#: One entry per VAT rate the invoice shows: {"rate": 8.1, "net": 100.0, "vat": 8.1}.
	vat_breakdown: list[dict[str, float]] = field(default_factory=list)
	#: {"description": str, "net": float, "vat_rate": float | None}, as printed.
	line_items: list[dict[str, Any]] = field(default_factory=list)


#: Options of `ePost Letter.document_kind`.
DOCUMENT_KINDS = (
	"Invoice",
	"Credit Note",
	"Reminder",
	"Receipt",
	"Contract",
	"Tax Assessment",
	"Correspondence",
	"Other",
)


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
