"""Create a DRAFT Purchase Invoice from an ePost letter.

The invoice is a starting point, never an auto-booking: it is left in draft, it
is never submitted, and every field the extractor could not fill is left empty
for a human. Nothing here talks to ePost.
"""

from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

DOCTYPE = "ePost Letter"

#: Stripped before comparing a letter's vendor name to a Supplier name.
LEGAL_SUFFIXES = {
	"ag",
	"sa",
	"sarl",
	"sagl",
	"gmbh",
	"ug",
	"kg",
	"ohg",
	"klg",
	"ltd",
	"limited",
	"llc",
	"inc",
	"corp",
	"co",
	"plc",
	"bv",
	"nv",
	"srl",
	"spa",
	"oy",
	"ab",
	"as",
}


@frappe.whitelist()
def create_purchase_invoice(letter_name: str, supplier: str | None = None) -> dict:
	"""Create a draft Purchase Invoice for `letter_name`."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("write")

	# The invoice below is inserted with `ignore_permissions`, so the right to
	# create one has to be established here. Without this, write access to an
	# ePost Letter — which a custom role can be given for triage alone — carries
	# the right to create Purchase Invoices with it.
	frappe.has_permission("Purchase Invoice", "create", throw=True)

	if letter.purchase_invoice and frappe.db.exists("Purchase Invoice", letter.purchase_invoice):
		frappe.throw(
			_("Letter {0} is already linked to Purchase Invoice {1}").format(
				letter.letter_id, letter.purchase_invoice
			)
		)

	settings = frappe.get_cached_doc("ePost Settings")
	company = settings.company or _default_company()
	if not company:
		frappe.throw(_("Set a Company in ePost Settings before importing letters"))

	supplier = supplier or letter.supplier or _suggestion(letter).get("supplier") or find_supplier(letter)
	if not supplier:
		frappe.throw(
			_(
				"No Supplier matches {0}. Pick one on the letter, or pass it to "
				"the Create Purchase Invoice dialog."
			).format(letter.vendor_name or letter.sender_name or letter.title)
		)

	invoice = _build_invoice(letter, settings, company, supplier)
	invoice.insert(ignore_permissions=True)

	_attach_letter_pdf(letter, invoice.name)

	letter.supplier = supplier
	letter.purchase_invoice = invoice.name
	letter.status = "Imported"
	letter.save(ignore_permissions=True)

	return {"purchase_invoice": invoice.name, "supplier": supplier, "status": letter.status}


@frappe.whitelist()
def suggest_supplier(letter_name: str) -> dict:
	"""What `create_purchase_invoice` would pick, for the Desk dialog default."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("read")
	return {"supplier": letter.supplier or _suggestion(letter).get("supplier") or find_supplier(letter)}


def find_supplier(letter: Any) -> str | None:
	"""Best-effort match of the letter's vendor against existing Suppliers.

	Exact match first, then a normalised comparison that ignores case,
	punctuation and legal-form suffixes. Deliberately conservative: an ambiguous
	match returns None so the user is asked instead of being handed a guess.
	"""
	candidates = [c for c in (letter.vendor_name, letter.sender_name) if c]
	if not candidates:
		return None

	for candidate in candidates:
		exact = frappe.db.get_value("Supplier", {"supplier_name": candidate}, "name")
		if exact:
			return exact

	suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"])
	for candidate in candidates:
		normalised = _normalise(candidate)
		if not normalised:
			continue

		matches = {s.name for s in suppliers if _normalise(s.supplier_name) == normalised}
		if len(matches) == 1:
			return matches.pop()

		if len(normalised) >= 4:
			matches = {
				s.name
				for s in suppliers
				if (other := _normalise(s.supplier_name)) and (normalised in other or other in normalised)
			}
			if len(matches) == 1:
				return matches.pop()

	return None


def _build_invoice(letter: Any, settings: Any, company: str, supplier: str):
	invoice = frappe.new_doc("Purchase Invoice")
	invoice.company = company
	invoice.supplier = supplier
	invoice.bill_no = letter.invoice_number
	invoice.bill_date = letter.invoice_date
	invoice.remarks = _("Imported from ePost letter {0}: {1}").format(letter.letter_id, letter.title or "")

	if letter.invoice_date:
		# posting_date only follows the document date when set_posting_time is on.
		invoice.set_posting_time = 1
		invoice.posting_date = letter.invoice_date

	posting_date = getdate(invoice.posting_date or today())
	if letter.due_date and getdate(letter.due_date) >= posting_date:
		invoice.due_date = letter.due_date

	_set_currency(invoice, letter, company, supplier, posting_date)

	suggestion = _suggestion(letter)
	lines = [line for line in suggestion.get("lines") or [] if flt(line.get("net"))]
	if lines:
		for line in lines:
			invoice.append("items", _suggested_item(letter, settings, company, line))
		invoice.remarks += "\n" + _("Booking suggested from: {0}").format(_sources(lines))
		template = suggestion.get("taxes_and_charges")
		if template and all(line.get("item_tax_template") for line in lines):
			_apply_taxes_template(invoice, template)
	else:
		invoice.append("items", _build_item(letter, settings, company))
	invoice.set_missing_values()
	return invoice


def _set_currency(invoice, letter: Any, company: str, supplier: str, posting_date) -> None:
	"""Denominate the draft in the letter's currency when ERPNext will accept it.

	`new_doc` fills `currency` from the site defaults, which know nothing about
	this company, so it is always set here. A foreign currency needs a rate, and
	ERPNext refuses one for a supplier that has no ledger entries yet unless its
	payable account is in that currency or multi-currency invoices are allowed.
	Where either is missing the draft stays in the company currency, and the
	currency read off the letter is noted for the human.
	"""
	company_currency = _company_currency(company)
	invoice.currency = company_currency
	invoice.conversion_rate = 1
	if not letter.currency or letter.currency == company_currency:
		return

	rate = _exchange_rate(letter.currency, company_currency, posting_date)
	if rate and _supplier_accepts_currency(supplier, company, letter.currency, company_currency):
		invoice.currency = letter.currency
		invoice.conversion_rate = rate
		return
	invoice.remarks += _("\nDetected currency on the letter: {0}").format(letter.currency)


def _supplier_accepts_currency(supplier: str, company: str, currency: str, company_currency: str) -> bool:
	from erpnext.accounts.party import get_party_account_currency, get_party_gle_currency

	account_currency = get_party_account_currency("Supplier", supplier, company)
	if account_currency and account_currency != company_currency:
		return currency == account_currency
	if get_party_gle_currency("Supplier", supplier, company):
		return True
	return account_currency == currency or bool(
		frappe.db.get_single_value(
			"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
		)
	)


def _exchange_rate(from_currency: str, to_currency: str, on) -> float:
	from erpnext.setup.utils import get_exchange_rate

	try:
		return flt(get_exchange_rate(from_currency, to_currency, on, "for_buying"))
	except Exception:
		return 0.0


def _suggestion(letter: Any) -> dict:
	try:
		value = json.loads(letter.booking_suggestion or "{}")
	except ValueError:
		return {}
	return value if isinstance(value, dict) else {}


def _suggested_item(letter: Any, settings: Any, company: str, line: dict) -> dict:
	item = _build_item(letter, settings, company)
	item["rate"] = flt(line.get("net"))
	if line.get("description"):
		item["description"] = line["description"]
	if line.get("expense_account"):
		item["expense_account"] = line["expense_account"]
	if line.get("item_tax_template"):
		item["item_tax_template"] = line["item_tax_template"]
	if line.get("cost_center"):
		item["cost_center"] = line["cost_center"]
	return item


def _apply_taxes_template(invoice, template: str) -> None:
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	invoice.taxes_and_charges = template
	invoice.set("taxes", [])
	for row in get_taxes_and_charges("Purchase Taxes and Charges Template", template) or []:
		# Suggested lines carry net amounts; the template adds the VAT on top.
		row["included_in_print_rate"] = 0
		invoice.append("taxes", row)


def _sources(lines: list[dict]) -> str:
	return ", ".join(
		f"{flt(line.get('net')):.2f} {line.get('account_source') or 'None'}"
		+ (f" ({line['evidence'][0]})" if line.get("evidence") else "")
		for line in lines
	)


def _build_item(letter: Any, settings: Any, company: str) -> dict:
	amount = flt(letter.amount)
	item: dict[str, Any] = {
		"qty": 1,
		"rate": amount,
		"description": letter.title or letter.letter_id,
		"uom": "Nos",
		"conversion_factor": 1,
	}

	default_item = getattr(settings, "default_item_code", None)
	if default_item:
		item["item_code"] = default_item
	else:
		# No Item configured: a non-stock line carrying the letter title, which
		# ERPNext accepts as long as an expense account is set.
		item["item_name"] = (letter.title or letter.letter_id)[:140]

	expense_account = settings.default_expense_account or _default_expense_account(company)
	if expense_account:
		item["expense_account"] = expense_account
	if settings.default_cost_center:
		item["cost_center"] = settings.default_cost_center

	return item


def _attach_letter_pdf(letter: Any, invoice_name: str) -> None:
	"""Link the letter's existing private File to the invoice.

	A second File row pointing at the same `file_url`, so the PDF is stored once.
	"""
	if not letter.file:
		return

	source = frappe.db.get_value("File", {"file_url": letter.file}, ["file_name"], as_dict=True)
	frappe.get_doc(
		{
			"doctype": "File",
			"file_url": letter.file,
			"file_name": (source and source.file_name) or f"{letter.letter_id}.pdf",
			"attached_to_doctype": "Purchase Invoice",
			"attached_to_name": invoice_name,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)


def _normalise(value: str | None) -> str:
	if not value:
		return ""
	cleaned = re.sub(r"[^a-z0-9\s]", " ", value.lower())
	words = [w for w in cleaned.split() if w and w not in LEGAL_SUFFIXES]
	return "".join(words)


def _default_company() -> str | None:
	from erpnext import get_default_company

	return get_default_company()


def _company_currency(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "default_currency")


def _default_expense_account(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "default_expense_account")
