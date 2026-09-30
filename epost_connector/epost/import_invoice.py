"""Create a DRAFT Purchase Invoice from an ePost letter.

One builder, used by the pipeline (`inbox/process.py`) and by the letter's
button. The draft is where review happens: every field stays editable in
ERPNext's own form, `review_notes` says what to check, and each line says where
its account came from. It is never submitted here. Nothing here talks to ePost.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from epost_connector.inbox import matching

DOCTYPE = "ePost Letter"
LOG_DOCTYPE = "ePost Extraction Log"

#: Plain-language text for each extraction check that failed.
CHECK_NOTES = {
	"totals_add_up": "Net + VAT does not equal the gross amount read off the letter.",
	"breakdown_matches_totals": "The VAT breakdown does not add up to the totals.",
	"breakdown_rates_consistent": "A VAT amount does not match its rate.",
	"dates_valid": "A date could not be read, or the due date is before the invoice date.",
	"currency_known": "The currency on the letter is not set up in ERPNext.",
	"gross_present_on_invoice": "No total amount was found on the invoice.",
	"amounts_only_on_financial_documents": "Amounts were read off a letter that is not an invoice.",
}


@frappe.whitelist()
def create_purchase_invoice(letter_name: str, supplier: str | None = None) -> dict:
	"""Desk button: create the draft for a letter whose supplier is known."""
	letter = frappe.get_doc(DOCTYPE, letter_name)
	letter.check_permission("write")
	supplier = supplier or letter.supplier or matching.find_supplier(letter)
	if not supplier:
		frappe.throw(
			_("No Supplier is set for {0}. Map or create one on the letter first.").format(
				letter.sender_name or letter.title
			)
		)
	invoice = create_draft(letter, supplier)
	return {"purchase_invoice": invoice.name, "supplier": supplier, "status": letter.status}


def create_draft(letter: Any, supplier: str):
	"""Build, insert and link the draft Purchase Invoice for `letter`."""
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

	log = _log(letter)
	invoice = _build_invoice(letter, log, settings, company, supplier)
	invoice.insert(ignore_permissions=True, set_name=_prompted_name(invoice))

	_attach_letter_pdf(letter, invoice.name)

	letter.supplier = supplier
	letter.purchase_invoice = invoice.name
	letter.status = "Drafted"
	letter.processing_note = None
	letter.save(ignore_permissions=True)
	return invoice


def find_supplier(letter: Any) -> str | None:
	"""Name-only supplier match; see `inbox/matching.py` for the full order."""
	return matching.find_supplier(letter)


def _build_invoice(letter: Any, log: Any, settings: Any, company: str, supplier: str):
	values = log or frappe._dict()
	notes: list[str] = []

	invoice = frappe.new_doc("Purchase Invoice")
	invoice.company = company
	invoice.supplier = supplier
	invoice.bill_no = values.invoice_number
	invoice.bill_date = values.invoice_date
	invoice.remarks = _("Imported from ePost letter {0}: {1}").format(letter.letter_id, letter.title or "")
	invoice.epost_letter = letter.name
	invoice.extraction_confidence = flt(values.confidence) * 100

	if values.invoice_date:
		# posting_date only follows the document date when set_posting_time is on.
		invoice.set_posting_time = 1
		invoice.posting_date = values.invoice_date

	posting_date = getdate(invoice.posting_date or today())
	if values.due_date and getdate(values.due_date) >= posting_date:
		invoice.due_date = values.due_date

	notes += _set_currency(invoice, values, company, supplier, posting_date)

	# A supplier's credit note lowers what is owed, so it is drafted as ERPNext's
	# debit note: a return with negative quantities. Amounts are taken as
	# magnitudes because letters print a credit either way round.
	if values.document_kind == "Credit Note":
		invoice.is_return = 1

	lines = []
	if log:
		from epost_connector.inbox.process import suggest_booking

		try:
			lines = [line for line in suggest_booking(letter, log, supplier).lines if flt(line.net)]
		except Exception:
			frappe.log_error(
				"ePost: booking suggestion failed", reference_doctype=DOCTYPE, reference_name=letter.name
			)
			notes.append(_("No booking suggestion: suggesting failed, see the Error Log."))

	if lines:
		for line in lines:
			invoice.append(
				"items", _signed(invoice, _suggested_item(letter, values, settings, company, line))
			)
		notes += _line_notes(lines)
		templates = {line.item_tax_template for line in lines}
		if None in templates:
			notes.append(_("VAT not applied: no VAT template could be suggested for every line."))
		else:
			from epost_connector.booking.suggest import _taxes_template

			template = _taxes_template(lines, settings.mixed_taxes_template)
			if template:
				notes += _apply_taxes_template(invoice, template)
			else:
				notes.append(_missing_taxes_note(templates))
	else:
		invoice.append("items", _signed(invoice, _build_item(letter, values, settings, company)))

	notes += _check_notes(log)
	invoice.review_notes = "\n".join(f"• {note}" for note in notes) or None
	invoice.set_missing_values()
	return invoice


def _set_currency(invoice, values: Any, company: str, supplier: str, posting_date) -> list[str]:
	"""Denominate the draft in the letter's currency when ERPNext will accept it.

	`new_doc` fills `currency` from the site defaults, which know nothing about
	this company, so it is always set here. A foreign currency needs a rate, and
	ERPNext refuses one for a supplier that has no ledger entries yet unless its
	payable account is in that currency or multi-currency invoices are allowed.
	Where either is missing the draft stays in the company currency and a note
	says what was read off the letter.
	"""
	company_currency = _company_currency(company)
	invoice.currency = company_currency
	invoice.conversion_rate = 1
	currency = values.currency
	if not currency or currency == company_currency:
		return []

	if _supplier_accepts_currency(supplier, company, currency, company_currency):
		rate = _exchange_rate(currency, company_currency, posting_date)
		if rate:
			invoice.currency = currency
			invoice.conversion_rate = rate
			return []
	return [
		_("The letter is in {0}; the draft is in {1}. Check the amounts.").format(currency, company_currency)
	]


def _supplier_accepts_currency(supplier: str, company: str, currency: str, company_currency: str) -> bool:
	from erpnext.accounts.party import get_party_account_currency, get_party_gle_currency

	account_currency = get_party_account_currency("Supplier", supplier, company)
	if account_currency and account_currency != company_currency:
		return currency == account_currency
	ledger_currency = get_party_gle_currency("Supplier", supplier, company)
	if ledger_currency:
		# Entries in the company currency leave the invoice currency free; entries
		# in another currency bind the supplier to it.
		return ledger_currency == company_currency or ledger_currency == currency
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


def _signed(invoice, item: dict) -> dict:
	if invoice.is_return:
		item["rate"] = abs(flt(item["rate"]))
		item["qty"] = -1
	return item


def _suggested_item(letter: Any, values: Any, settings: Any, company: str, line) -> dict:
	item = _build_item(letter, values, settings, company)
	item["rate"] = flt(line.net)
	if line.description:
		item["description"] = line.description
	if line.expense_account:
		item["expense_account"] = line.expense_account
	if line.item_tax_template:
		item["item_tax_template"] = line.item_tax_template
	if line.cost_center:
		item["cost_center"] = line.cost_center
	item["booking_source"] = line.account_source
	item["booking_confidence"] = flt(line.confidence) * 100
	return item


def _line_notes(lines: list) -> list[str]:
	notes = []
	for line in lines:
		if line.account_source == "None":
			notes.append(
				_("{0}: no account could be suggested; the default was used.").format(_money(line.net))
			)
		elif line.account_source == "LLM":
			notes.append(
				_("{0}: account chosen by the model, the supplier has no clear history.").format(
					_money(line.net)
				)
			)
		elif line.account_source == "History" and flt(line.confidence) < 0.9:
			notes.append(
				_("{0}: the supplier was booked more than one way before ({1:.0%} this way).").format(
					_money(line.net), flt(line.confidence)
				)
			)
	return notes


def _check_notes(log: Any) -> list[str]:
	if not log:
		return [_("Nothing was extracted from the letter; fill in the amounts from the PDF.")]
	try:
		checks = (json.loads(log.answer or "{}") or {}).get("checks") or {}
	except ValueError:
		return []
	return [
		_(CHECK_NOTES[name]) for name, passed in checks.items() if passed is False and name in CHECK_NOTES
	]


def _missing_taxes_note(templates: set[str]) -> str:
	if len(templates) > 1:
		return _(
			"VAT not applied: the lines use different VAT templates and ePost Settings has no Mixed Purchase Taxes Template."
		)
	return _("VAT not applied: there is no Purchase Taxes and Charges Template named {0}.").format(
		next(iter(templates))
	)


def _apply_taxes_template(invoice, template: str) -> list[str]:
	from erpnext.controllers.accounts_controller import get_taxes_and_charges

	try:
		rows = get_taxes_and_charges("Purchase Taxes and Charges Template", template) or []
	except frappe.DoesNotExistError:
		return [
			_("VAT not applied: Purchase Taxes and Charges Template {0} no longer exists.").format(template)
		]
	invoice.taxes_and_charges = template
	invoice.set("taxes", [])
	for row in rows:
		# Suggested lines carry net amounts; the template adds the VAT on top.
		row["included_in_print_rate"] = 0
		invoice.append("taxes", row)
	return []


def _build_item(letter: Any, values: Any, settings: Any, company: str) -> dict:
	amount = flt(values.gross_amount if values.gross_amount is not None else values.net_amount)
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


def _prompted_name(invoice) -> str | None:
	"""A name for sites that name Purchase Invoices by prompt, else None.

	A site that migrated its old invoice numbers typically sets `autoname` to
	`prompt` so they keep those names. Insert then refuses a document without a
	name, so the draft takes the next number of ERPNext's own naming series.
	"""
	meta = frappe.get_meta("Purchase Invoice")
	if (meta.autoname or "").lower() != "prompt":
		return None
	from frappe.model.naming import make_autoname

	options = [o for o in (meta.get_field("naming_series").options or "").split("\n") if o.strip()]
	returns = [o for o in options if "RET" in o.upper()]
	series = invoice.naming_series or ((returns or options)[0] if invoice.is_return else options[0])
	if "#" not in series:
		series = f"{series.rstrip('.')}.#####"
	return make_autoname(series, "Purchase Invoice", invoice)


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


def _log(letter: Any):
	if letter.extraction_log and frappe.db.exists(LOG_DOCTYPE, letter.extraction_log):
		return frappe.get_doc(LOG_DOCTYPE, letter.extraction_log)
	return None


def _money(value) -> str:
	return f"{flt(value):,.2f}"


def _default_company() -> str | None:
	from erpnext import get_default_company

	return get_default_company()


def _company_currency(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "default_currency")


def _default_expense_account(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "default_expense_account")
