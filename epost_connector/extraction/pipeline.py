"""The extract stage: run the configured extractor and persist what it found."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt, getdate

from epost_connector.extraction.base import ExtractionResult
from epost_connector.extraction.noop import NoopExtractor
from epost_connector.extraction.registry import get_extractor


def analyze_letter(letter: Any, force: bool = False) -> bool:
	"""Extract from `letter`'s PDF and write the result onto the doc.

	Returns True when an extractor produced a result. Runs only on letters that
	have a downloaded PDF and have not been analysed yet, unless `force`.
	"""
	if not letter.file:
		return False
	if not force and letter.status != "Downloaded":
		return False

	extractor = get_extractor(frappe.db.get_single_value("ePost Settings", "extractor"))
	if isinstance(extractor, NoopExtractor):
		# Short-circuit before touching the disk: the default configuration runs
		# on every letter of every hourly sync.
		return False

	result = extractor.extract(letter, _read_pdf(letter))
	if result is None:
		return False

	_apply(letter, result)
	letter.status = "Analyzed"
	letter.save(ignore_permissions=True)
	return True


@frappe.whitelist()
def analyze(letter_name: str) -> dict:
	"""Desk button: re-run extraction on one letter."""
	letter = frappe.get_doc("ePost Letter", letter_name)
	letter.check_permission("write")

	extracted = analyze_letter(letter, force=True)
	return {"extracted": extracted, "status": letter.status}


def _apply(letter: Any, result: ExtractionResult) -> None:
	letter.vendor_name = result.vendor_name
	letter.invoice_number = result.invoice_number
	letter.invoice_date = _as_date(result.invoice_date)
	letter.due_date = _as_date(result.due_date)
	letter.amount = flt(result.gross_amount if result.gross_amount is not None else result.net_amount)
	letter.vat_amount = flt(result.vat_amount)
	letter.extraction_confidence = flt(result.confidence)
	letter.extraction_raw = frappe.as_json(result.raw or {})

	# `currency` is a Link; an unknown code would fail validation on save.
	if result.currency and frappe.db.exists("Currency", result.currency):
		letter.currency = result.currency


def _as_date(value: Any):
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


def _read_pdf(letter: Any) -> bytes:
	"""Read the attached private file as bytes.

	Not `File.get_content()`: that tries a list of text encodings first and can
	hand back a mojibake string for a PDF.
	"""
	file_doc = frappe.get_doc("File", {"file_url": letter.file})
	with open(file_doc.get_full_path(), "rb") as handle:
		return handle.read()
