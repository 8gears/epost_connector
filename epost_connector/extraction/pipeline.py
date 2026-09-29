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
	_suggest_booking(letter)
	letter.save(ignore_permissions=True)
	return True


@frappe.whitelist()
def analyze(letter_name: str) -> dict:
	"""Desk button: re-run extraction on one letter."""
	letter = frappe.get_doc("ePost Letter", letter_name)
	letter.check_permission("write")

	extracted = analyze_letter(letter, force=True)
	return {"extracted": extracted, "status": letter.status}


#: One id for every backfill, so a second press is recognised as the same work.
ANALYZE_JOB_ID = "epost-analyze-all"


@frappe.whitelist()
def analyze_all(limit: int | None = None) -> dict:
	"""Desk button: extract every downloaded letter that has not been analysed yet.

	The hourly sync only extracts letters it downloads in that run, so letters
	synced before an extractor was configured need this once. `limit` runs the
	newest few first, to check a model on a sample before paying for the rest.
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))
	from frappe.utils.background_jobs import is_job_enqueued

	if is_job_enqueued(ANALYZE_JOB_ID):
		return {"job_id": ANALYZE_JOB_ID, "already_running": True}

	frappe.enqueue(
		"epost_connector.extraction.pipeline.analyze_downloaded",
		queue="long",
		timeout=7200,
		job_id=ANALYZE_JOB_ID,
		deduplicate=True,
		limit=int(limit) if limit else None,
	)
	return {"job_id": ANALYZE_JOB_ID, "already_running": False}


def analyze_downloaded(limit: int | None = None) -> dict:
	"""Analyse `Downloaded` letters newest first, committing after each one."""
	names = frappe.get_all(
		"ePost Letter",
		filters={"status": "Downloaded", "file": ("is", "set")},
		pluck="name",
		order_by="received_at desc",
		limit=limit or 0,
	)
	counts = {"letters": len(names), "analyzed": 0, "empty": 0, "failed": 0}
	for name in names:
		savepoint = f"epost_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		try:
			extracted = analyze_letter(frappe.get_doc("ePost Letter", name))
			frappe.db.commit()
			counts["analyzed" if extracted else "empty"] += 1
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error("ePost: analysis failed", reference_doctype="ePost Letter", reference_name=name)
			counts["failed"] += 1
	return counts


def _apply(letter: Any, result: ExtractionResult) -> None:
	letter.vendor_name = result.vendor_name
	letter.invoice_number = result.invoice_number
	letter.invoice_date = _as_date(result.invoice_date)
	letter.due_date = _as_date(result.due_date)
	letter.amount = flt(result.gross_amount if result.gross_amount is not None else result.net_amount)
	letter.vat_amount = flt(result.vat_amount)
	letter.extraction_confidence = flt(result.confidence)
	letter.extraction_raw = frappe.as_json(result.raw or {})
	letter.document_kind = result.document_kind
	letter.vendor_tax_id = result.vendor_tax_id
	letter.vendor_country = (
		result.vendor_country
		if result.vendor_country and frappe.db.exists("Country", result.vendor_country)
		else None
	)
	letter.iban = result.iban
	letter.qr_reference = result.qr_reference
	letter.net_amount = flt(result.net_amount) if result.net_amount is not None else None

	# Assigned unconditionally like every other field above, so the letter shows
	# what this extractor found rather than what a previous run left behind.
	# `currency` is a Link, so a code ERPNext has no Currency for is dropped:
	# keeping it would fail the save and lose the rest of the result with it.
	known = bool(result.currency) and frappe.db.exists("Currency", result.currency)
	letter.currency = result.currency if known else None


def _suggest_booking(letter: Any) -> None:
	"""Best effort: a failed suggestion must not cost the extraction it follows.

	The suggestion only reads, so there is nothing to roll back.
	"""
	from epost_connector.booking.letter import suggest_for_letter

	try:
		suggest_for_letter(letter)
	except Exception:
		frappe.log_error(
			"ePost: booking suggestion failed", reference_doctype="ePost Letter", reference_name=letter.name
		)


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
