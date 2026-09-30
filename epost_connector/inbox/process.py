"""Take a downloaded letter as far as a draft Purchase Invoice, or say why not.

    extract -> route (rules) -> supplier -> duplicate check -> draft invoice

Each step can stop the letter with a status and a note a human can act on:
`Not Bookable` (a rule or no amount), `Waiting for Supplier` (nothing matched;
the reviewer maps or creates one, which continues here), `Duplicate` (an
invoice with that supplier and number exists). Nothing here books: the result
is a draft the reviewer checks and submits in the Purchase Invoice form.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from epost_connector.booking.suggest import BookingContext, BookingSuggestion, VatGroup, route, suggest
from epost_connector.extraction.pipeline import extract_letter
from epost_connector.inbox import matching

DOCTYPE = "ePost Letter"
LOG_DOCTYPE = "ePost Extraction Log"


def process_letter(letter: Any, force: bool = False, confirmed: bool = False) -> bool:
	"""Run the pipeline on one letter and save it. True when an extraction exists.

	A letter the pipeline has finished with is left alone unless `force`: a
	drafted letter re-run would find its own invoice and call itself a duplicate.
	"""
	from epost_connector.epost.sync import TERMINAL_STATUSES

	if letter.status in TERMINAL_STATUSES and not force:
		return False
	letter.flags.in_processing = True
	log = extract_letter(letter, force=force)
	if not log:
		return False
	letter.extraction_log = log.name
	settings = frappe.get_cached_doc("ePost Settings")
	company = settings.company or _default_company()
	context = build_context(letter, log, company, supplier=letter.supplier)

	decided = route(context)
	if decided.status:
		return _stop(letter, decided.status, _("Rule {0}").format(decided.rule))
	if not (log.gross_amount or log.net_amount):
		return _stop(letter, "Not Bookable", _("No amount was read off the letter."))

	if letter.supplier:
		supplier, source, detail = letter.supplier, "Reviewer" if confirmed else "Letter", None
	elif decided.supplier:
		supplier, source, detail = decided.supplier, "Rule", decided.rule
	else:
		model = settings.flow_model if settings.booking_use_llm else None
		match = matching.find(letter, log, model=model)
		if not match.supplier:
			return _stop(letter, "Waiting for Supplier", _waiting_note(log, match))
		supplier, source, detail = match.supplier, match.source, match.reason
	if confirmed or source == "LLM":
		matching.learn(supplier, log, confirmed=confirmed)
	letter.supplier = supplier
	letter.flags.supplier_found = _supplier_note(source, detail, log)

	existing = find_duplicate(supplier, log.invoice_number)
	if existing:
		return _stop(letter, "Duplicate", _("Already recorded as Purchase Invoice {0}.").format(existing))

	if log.invoice_date and not in_fiscal_year(log.invoice_date, company):
		return _stop(
			letter,
			"Not Bookable",
			_(
				"The invoice date {0} is in no fiscal year of {1}; it belongs to the books kept before."
			).format(log.invoice_date, company),
		)

	letter.processing_note = None
	letter.save(ignore_permissions=True)

	from epost_connector.epost.import_invoice import create_draft

	create_draft(letter, supplier)
	return True


def process_letter_by_name(letter_name: str, confirmed: bool = False) -> None:
	"""Background job: continue a letter after a reviewer chose its supplier."""
	process_letter(frappe.get_doc(DOCTYPE, letter_name), confirmed=confirmed)
	frappe.db.commit()


def process_downloaded(limit: int | None = None) -> dict:
	"""Process `Downloaded` letters newest first, committing after each one."""
	names = frappe.get_all(
		DOCTYPE,
		filters={"status": "Downloaded", "file": ("is", "set")},
		pluck="name",
		order_by="received_at desc",
		limit=cint(limit) or 0,
	)
	counts = {"letters": len(names), "processed": 0, "empty": 0, "failed": 0}
	for name in names:
		savepoint = f"epost_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		try:
			done = process_letter(frappe.get_doc(DOCTYPE, name))
			frappe.db.commit()
			counts["processed" if done else "empty"] += 1
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error("ePost: processing failed", reference_doctype=DOCTYPE, reference_name=name)
			counts["failed"] += 1
	return counts


@frappe.whitelist()
def process(letter_name: str, force: int = 0) -> dict:
	"""Desk button: run (or with `force`, re-run from extraction) the pipeline."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("write")
	if letter.status == "Drafted":
		frappe.throw(_("This letter already has a draft: {0}").format(letter.purchase_invoice))
	if cint(force) and letter.status not in ("New", "Downloaded"):
		letter.flags.reprocess = True
		letter.status = "Downloaded"
	process_letter(letter, force=bool(cint(force)))
	return {"status": letter.status, "purchase_invoice": letter.purchase_invoice}


@frappe.whitelist()
def supplier_hints(letter_name: str) -> dict:
	"""What the Map and Create supplier dialogs show: the issuer as read, and guesses."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("read")
	log = _log(letter)
	names = [n for n in ((log and log.vendor_name), letter.sender_name) if n]
	return {
		"vendor_name": log.vendor_name if log else None,
		"vendor_tax_id": log.vendor_tax_id if log else None,
		"vendor_country": log.vendor_country if log else None,
		"vendor_address": log.vendor_address if log else None,
		"iban": log.iban if log else None,
		"candidates": matching.closest(names, limit=5),
		"note": letter.processing_note,
	}


@frappe.whitelist()
def create_supplier(
	letter_name: str, supplier_name: str, tax_id: str | None = None, country: str | None = None
) -> dict:
	"""Create dialog: a Supplier from what the letter shows, then continue the letter."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("write")
	frappe.has_permission("Supplier", "create", throw=True)
	supplier = frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": supplier_name, "tax_id": tax_id, "country": country}
	).insert()
	letter.supplier = supplier.name
	letter.save()
	return {"supplier": supplier.name}


def build_context(letter: Any, log: Any, company: str, supplier: str | None = None) -> BookingContext:
	extras = _extras(log)
	return BookingContext(
		company=company,
		supplier=supplier,
		vendor_name=log.vendor_name or letter.sender_name,
		vendor_tax_id=log.vendor_tax_id,
		vendor_country=log.vendor_country
		or (supplier and frappe.db.get_value("Supplier", supplier, "country")),
		document_kind=log.document_kind,
		text="\n".join(filter(None, (letter.title, letter.sender_name, extras.get("summary")))),
		groups=vat_groups(log, extras, letter.title),
	)


def suggest_booking(letter: Any, log: Any, supplier: str) -> BookingSuggestion:
	"""The booking for the draft, computed when the draft is built, never stored."""
	settings = frappe.get_cached_doc("ePost Settings")
	company = settings.company or _default_company()
	return suggest(
		build_context(letter, log, company, supplier=supplier),
		mixed_template=settings.mixed_taxes_template,
		llm_model=settings.flow_model if settings.booking_use_llm else None,
	)


def vat_groups(log: Any, extras: dict, fallback: str | None = None) -> list[VatGroup]:
	"""One group per VAT rate read off the letter.

	Without a breakdown the whole net amount is one group. Its rate is derived
	from the totals when they allow it, and a gross amount with no VAT on top is
	0 %, which on a foreign invoice is the reverse-charge case.
	"""
	items = extras.get("line_items")
	descriptions = _descriptions_by_rate(items if isinstance(items, list) else [])
	breakdown = extras.get("vat_breakdown")
	rows = [
		r
		for r in (breakdown if isinstance(breakdown, list) else [])
		if isinstance(r, dict) and flt(r.get("net"))
	]
	if rows:
		return [
			VatGroup(
				net=flt(r["net"]),
				rate=flt(r.get("rate")),
				description=descriptions.get(round(flt(r.get("rate")), 2)) or extras.get("summary"),
			)
			for r in rows
		]

	gross, vat = log.gross_amount, log.vat_amount
	net = flt(log.net_amount) or (flt(gross) - flt(vat))
	rate = round(flt(vat) / net * 100, 1) if net and vat is not None and gross else None
	return [VatGroup(net=net, rate=rate, description=extras.get("summary") or fallback)]


def in_fiscal_year(date, company: str) -> bool:
	from erpnext.accounts.utils import get_fiscal_year

	return bool(get_fiscal_year(date, company=company, boolean=True))


def find_duplicate(supplier: str, bill_no: str | None) -> str | None:
	if not bill_no:
		return None
	return frappe.db.get_value(
		"Purchase Invoice", {"supplier": supplier, "bill_no": bill_no, "docstatus": ("<", 2)}, "name"
	)


def _stop(letter: Any, status: str, note: str) -> bool:
	letter.status = status
	letter.processing_note = note
	letter.save(ignore_permissions=True)
	return True


def _supplier_note(source: str, detail: str | None, log: Any) -> str:
	"""How the supplier was found, in words, for the draft's review notes."""
	name = log.vendor_name or _("the issuer")
	notes = {
		"Reviewer": _("Supplier chosen by a reviewer for {0}.").format(name),
		"Letter": _("Supplier was already set on the letter; {0} was not matched.").format(name),
		"Rule": _("Supplier set by rule {0}.").format(detail),
		"Alias": _("Supplier matched by a learned name, VAT id or IBAN for {0}. Check it.").format(name),
		"Tax ID": _("Supplier matched by VAT id {0}.").format(log.vendor_tax_id),
		"Name": _("Supplier matched by name: {0}.").format(name),
		"IBAN": _("Supplier matched by IBAN {0}.").format(log.iban),
		"LLM": _("Supplier chosen by the model for {0}: {1} Check it.").format(name, detail or ""),
	}
	return notes.get(source, "")


def _waiting_note(log: Any, match: matching.Match) -> str:
	issuer = " ".join(
		filter(None, (log.vendor_name, f"({log.vendor_tax_id})" if log.vendor_tax_id else None))
	)
	parts = [_("No supplier matched {0}.").format(issuer or _("the issuer"))]
	if match.candidates:
		parts.append(_("Closest: {0}.").format(", ".join(c["supplier"] for c in match.candidates)))
	if match.reason:
		parts.append(_("Model: {0}").format(match.reason))
	return " ".join(parts)


def _extras(log: Any) -> dict:
	try:
		value = json.loads(log.answer or "{}")
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _log(letter: Any):
	if letter.extraction_log and frappe.db.exists(LOG_DOCTYPE, letter.extraction_log):
		return frappe.get_doc(LOG_DOCTYPE, letter.extraction_log)
	return None


def _descriptions_by_rate(items: list) -> dict[float, str]:
	grouped: dict[float, list[str]] = {}
	for item in items:
		if isinstance(item, dict) and item.get("description") and item.get("vat_rate") is not None:
			grouped.setdefault(round(flt(item["vat_rate"]), 2), []).append(str(item["description"]))
	return {rate: "; ".join(texts[:3])[:280] for rate, texts in grouped.items()}


def _default_company() -> str | None:
	from erpnext import get_default_company

	return get_default_company()
