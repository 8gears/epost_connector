"""Suggest a booking for an extracted ePost Letter and keep it on the letter."""

from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import flt

from epost_connector.booking.suggest import BookingContext, BookingSuggestion, VatGroup, suggest

DOCTYPE = "ePost Letter"

#: Letters worth a booking suggestion. A contract or a tax assessment is not
#: booked from the letter, whatever amounts it mentions.
BOOKABLE_KINDS = {None, "", "Invoice", "Credit Note", "Receipt"}


@frappe.whitelist()
def refresh(letter_name: str) -> dict:
	"""Desk action: suggest the booking again, e.g. after a rule was added."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("write")
	suggestion = suggest_for_letter(letter)
	letter.save(ignore_permissions=True)
	return suggestion.as_dict() if suggestion else {}


def suggest_for_letter(letter: Any) -> BookingSuggestion | None:
	"""Suggest, write the result onto `letter` (unsaved), and return it.

	The previous suggestion is cleared first. A letter re-read as a contract, or
	with its amounts gone, must not keep lines the importer would still use.
	"""
	letter.booking_suggestion = None
	letter.booking_source = None
	letter.booking_confidence = 0
	if letter.document_kind not in BOOKABLE_KINDS or not (letter.amount or letter.net_amount):
		return None

	settings = frappe.get_cached_doc("ePost Settings")
	company = settings.company or _default_company()
	if not company:
		return None

	answer = _answer(letter)
	supplier, supplier_source = match_supplier(letter)
	context = BookingContext(
		company=company,
		supplier=supplier,
		vendor_name=letter.vendor_name or letter.sender_name,
		vendor_tax_id=letter.vendor_tax_id,
		vendor_country=letter.vendor_country
		or (supplier and frappe.db.get_value("Supplier", supplier, "country")),
		text="\n".join(filter(None, (letter.title, answer.get("summary")))),
		groups=vat_groups(letter, answer),
	)
	suggestion = suggest(
		context,
		mixed_template=settings.mixed_taxes_template,
		llm_model=settings.flow_model if settings.booking_use_llm else None,
	)

	stored = suggestion.as_dict()
	stored["supplier"] = supplier
	stored["supplier_source"] = supplier_source
	letter.booking_suggestion = json.dumps(stored, indent=1, default=str)
	letter.booking_source = suggestion.source
	letter.booking_confidence = min((line.confidence for line in suggestion.lines), default=0.0)
	return suggestion


def vat_groups(letter: Any, answer: dict) -> list[VatGroup]:
	"""One group per VAT rate read off the letter.

	Without a breakdown the whole net amount is one group. Its rate is derived
	from the totals when they allow it, and a gross amount with no VAT on top is
	0 %, which on a foreign invoice is the reverse-charge case.
	"""
	items = answer.get("line_items")
	descriptions = _descriptions_by_rate(items if isinstance(items, list) else [])
	breakdown = answer.get("vat_breakdown")
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
				description=descriptions.get(round(flt(r.get("rate")), 2)) or answer.get("summary"),
			)
			for r in rows
		]

	net = flt(letter.net_amount) or (flt(letter.amount) - flt(letter.vat_amount))
	rate = None
	if net and letter.vat_amount is not None and letter.amount:
		rate = round(flt(letter.vat_amount) / net * 100, 1)
	return [VatGroup(net=net, rate=rate, description=answer.get("summary") or letter.title)]


def match_supplier(letter: Any) -> tuple[str | None, str | None]:
	"""The Supplier this letter is from, and how it was found.

	The tax id first, because it is unique per company and printed on every
	invoice. Then the name match the importer has always used, then the IBAN the
	invoice asks to be paid to.
	"""
	if letter.supplier:
		return letter.supplier, "Letter"

	if letter.vendor_tax_id:
		wanted = _compact(letter.vendor_tax_id)
		for name, tax_id in frappe.get_all(
			"Supplier", filters={"tax_id": ("is", "set")}, fields=["name", "tax_id"], as_list=True
		):
			if _compact(tax_id) == wanted:
				return name, "Tax ID"

	from epost_connector.epost.import_invoice import find_supplier

	found = find_supplier(letter)
	if found:
		return found, "Name"

	if letter.iban:
		owners = set(
			frappe.get_all(
				"Bank Account",
				filters={"party_type": "Supplier", "iban": ("in", _iban_variants(letter.iban))},
				pluck="party",
			)
		)
		if len(owners) == 1:
			return owners.pop(), "IBAN"
	return None, None


def _answer(letter: Any) -> dict:
	try:
		raw = json.loads(letter.extraction_raw or "{}")
	except ValueError:
		return {}
	answer = raw.get("answer") if isinstance(raw, dict) else None
	return answer if isinstance(answer, dict) else {}


def _descriptions_by_rate(items: list) -> dict[float, str]:
	grouped: dict[float, list[str]] = {}
	for item in items:
		if isinstance(item, dict) and item.get("description") and item.get("vat_rate") is not None:
			grouped.setdefault(round(flt(item["vat_rate"]), 2), []).append(str(item["description"]))
	return {rate: "; ".join(texts[:3])[:280] for rate, texts in grouped.items()}


def _compact(value: str | None) -> str:
	return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _iban_variants(iban: str) -> list[str]:
	compact = _compact(iban)
	spaced = " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))
	return [iban, compact, spaced]


def _default_company() -> str | None:
	from erpnext import get_default_company

	return get_default_company()
