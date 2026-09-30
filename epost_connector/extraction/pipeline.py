"""The extract stage: run the configured extractor and keep what it found.

The result goes to an `ePost Extraction Log`, never onto the letter. The letter
stays what ePost delivered; the log is where the extracted values wait until a
supplier is known, and what the invoice builder reads.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from epost_connector.extraction.base import ExtractionResult
from epost_connector.extraction.noop import NoopExtractor
from epost_connector.extraction.registry import get_extractor

LOG_DOCTYPE = "ePost Extraction Log"


def extract_letter(letter: Any, force: bool = False):
	"""The letter's extraction log, extracting first when there is none or `force`.

	Returns None when no extractor is configured, the letter has no PDF, or the
	extractor read nothing. The caller links the log and saves the letter.
	"""
	if not letter.file:
		return None
	if letter.extraction_log and not force and frappe.db.exists(LOG_DOCTYPE, letter.extraction_log):
		return frappe.get_doc(LOG_DOCTYPE, letter.extraction_log)

	extractor = get_extractor(frappe.db.get_single_value("ePost Settings", "extractor"))
	if isinstance(extractor, NoopExtractor):
		# Short-circuit before touching the disk: the default configuration runs
		# on every letter of every hourly sync.
		return None

	result = extractor.extract(letter, _read_pdf(letter))
	if result is None:
		return None

	log = frappe.new_doc(LOG_DOCTYPE)
	log.letter = letter.name
	log.kind = "Extraction"
	log.status = "Success"
	_apply(log, result)
	log.insert(ignore_permissions=True)
	return log


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

	if isinstance(get_extractor(frappe.db.get_single_value("ePost Settings", "extractor")), NoopExtractor):
		frappe.throw(_("Choose an extractor in ePost Settings first; with None there is nothing to analyse."))

	if is_job_enqueued(ANALYZE_JOB_ID):
		return {"job_id": ANALYZE_JOB_ID, "already_running": True}

	frappe.enqueue(
		"epost_connector.inbox.process.process_downloaded",
		queue="long",
		timeout=7200,
		job_id=ANALYZE_JOB_ID,
		deduplicate=True,
		limit=cint(limit) or None,
	)
	return {"job_id": ANALYZE_JOB_ID, "already_running": False}


def _apply(log: Any, result: ExtractionResult) -> None:
	raw = result.raw or {}
	usage = raw.get("usage") or {}
	log.model = raw.get("model")
	log.input_type = raw.get("input")
	log.prompt_tokens = cint(usage.get("prompt_tokens"))
	log.completion_tokens = cint(usage.get("completion_tokens"))
	log.answer = json.dumps(
		{
			"answer": raw.get("answer"),
			"checks": raw.get("checks"),
			"vat_breakdown": result.vat_breakdown,
			"line_items": result.line_items,
			"summary": result.summary,
		},
		indent=1,
		default=str,
	)

	log.document_kind = result.document_kind
	log.vendor_name = result.vendor_name
	log.vendor_tax_id = result.vendor_tax_id
	log.vendor_address = result.vendor_address
	log.iban = result.iban
	log.qr_reference = result.qr_reference
	log.invoice_number = result.invoice_number
	log.invoice_date = _as_date(result.invoice_date)
	log.due_date = _as_date(result.due_date)
	log.service_period_from = _as_date(result.service_period_from)
	log.service_period_to = _as_date(result.service_period_to)
	log.net_amount = _amount(result.net_amount)
	log.vat_amount = _amount(result.vat_amount)
	log.gross_amount = _amount(result.gross_amount)
	log.confidence = flt(result.confidence)
	# Stored as text so Frappe's defaults cannot fill them on insert, and kept
	# only when ERPNext has a record for the value.
	log.vendor_country = (
		result.vendor_country
		if result.vendor_country and frappe.db.exists("Country", result.vendor_country)
		else None
	)
	log.currency = (
		result.currency if result.currency and frappe.db.exists("Currency", result.currency) else None
	)


def _amount(value):
	return None if value is None else flt(value)


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
