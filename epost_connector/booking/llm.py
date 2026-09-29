"""Ask a Flow model to book the lines that rules and history left open.

The model chooses; it does not invent. It gets the accounts and VAT templates
this company has actually booked supplier invoices to, and how every known
supplier was booked, and must answer with names from those lists. Any other
answer is dropped and the line stays open for a human.

Frappe Flow is an optional app. Without it, or without a working model, nothing
is filled and nothing raises: a draft with an open line is the same outcome as
before this module existed.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date

import frappe
from frappe.utils import flt, getdate

from epost_connector.ai import ask_json
from epost_connector.booking import history

#: A model's own certainty is not calibrated, so it never outranks history.
MAX_CONFIDENCE = 0.6

#: Suppliers shown as booking examples, heaviest first. About 40 tokens each.
MAX_EXAMPLES = 150

MAX_TEXT_CHARS = 6000

SCHEMA = {
	"type": "object",
	"properties": {
		"lines": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"index": {"type": "integer"},
					"expense_account": {"type": "string"},
					"item_tax_template": {"type": "string"},
					"confidence": {"type": "number"},
					"reason": {"type": "string"},
				},
				"required": ["index", "expense_account", "item_tax_template", "confidence", "reason"],
				"additionalProperties": False,
			},
		}
	},
	"required": ["lines"],
	"additionalProperties": False,
}

INSTRUCTIONS = """You book supplier invoices for the accounting team of {company}.
Choose an expense account and a VAT template for each open invoice line.

Rules:
- Answer only with an account from ALLOWED ACCOUNTS and a template from ALLOWED TEMPLATES, copied exactly.
- Prefer how similar suppliers were booked (BOOKING EXAMPLES): same kind of service, same country.
- The VAT rate the supplier charged on the line decides the template family: a line with VAT charged uses an input-tax template whose charged rate equals that rate; a foreign supplier charging no VAT is usually reverse charge (charged rate 0).
- confidence is 0 to 1. Use low values when the invoice gives little to go on.
- reason: one short sentence naming the example you followed."""


def fill(
	context,
	lines: list,
	model: str,
	as_of: date | str | None = None,
	exclude_invoices: Iterable[str] = (),
) -> Iterator[tuple[object, float]]:
	"""Fill `lines` in place from one model call. Yields (line, confidence) per line filled."""
	accounts, templates, examples = _choices(context.company, as_of, set(exclude_invoices))
	if not accounts:
		return

	answer = _ask(model, _prompt(context, lines, accounts, templates, examples))
	if not answer:
		return

	for row in answer.get("lines") or []:
		index = row.get("index")
		if not isinstance(index, int) or not 0 <= index < len(lines):
			continue
		account, template = row.get("expense_account"), row.get("item_tax_template")
		if account not in accounts or (template and template not in templates):
			continue
		line = lines[index]
		line.expense_account = account
		line.account_source = "LLM"
		if template and not line.item_tax_template:
			line.item_tax_template = template
			line.template_source = "LLM"
		if row.get("reason"):
			line.evidence.append(f"LLM: {str(row['reason'])[:200]}")
		yield line, min(max(flt(row.get("confidence")), 0.0), MAX_CONFIDENCE)


def _choices(company: str, as_of, excluded: set[str]) -> tuple[dict, dict, list[str]]:
	"""Accounts and templates in use, and one booking example per supplier."""
	filters = {"docstatus": 1, "company": company}
	if as_of:
		filters["posting_date"] = ("<", getdate(as_of))
	invoices = frappe.get_all(
		"Purchase Invoice", filters=filters, fields=["name", "supplier", "posting_date"]
	)
	invoices = [inv for inv in invoices if inv.name not in excluded]
	if not invoices:
		return {}, {}, []

	supplier_of = {inv.name: inv.supplier for inv in invoices}
	rows = frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": ("in", list(supplier_of)), "parenttype": "Purchase Invoice"},
		fields=["parent", "expense_account", "item_tax_template", "cost_center", "base_net_amount"],
	)
	dates = {inv.name: inv.posting_date for inv in invoices}
	by_supplier: dict[str, list[history.PastLine]] = {}
	for row in rows:
		if not row.expense_account:
			continue
		by_supplier.setdefault(supplier_of[row.parent], []).append(
			history.PastLine(
				invoice=row.parent,
				posting_date=getdate(dates[row.parent]),
				expense_account=row.expense_account,
				item_tax_template=row.item_tax_template or None,
				cost_center=row.cost_center or None,
				amount=flt(row.base_net_amount),
			)
		)

	account_names = {r.expense_account for r in rows if r.expense_account}
	template_names = {r.item_tax_template for r in rows if r.item_tax_template}
	accounts = {
		a.name: a.account_name
		for a in frappe.get_all(
			"Account",
			filters={"name": ("in", list(account_names)), "is_group": 0, "disabled": 0},
			fields=["name", "account_name"],
		)
	}
	templates = {t: history.charged_rate(t) for t in template_names}

	countries = dict(frappe.get_all("Supplier", fields=["name", "country"], as_list=True))
	weighted = []
	for supplier, past in by_supplier.items():
		ranked = history.rank(past, as_of)
		top = ranked[0]
		weighted.append(
			(
				sum(c.weight for c in ranked),
				f"{supplier} | {countries.get(supplier) or '?'} | {top.expense_account} | "
				f"{top.item_tax_template or '-'} | share {top.share:.2f} | {len(top.invoices)} invoices",
			)
		)
	examples = [text for _, text in sorted(weighted, reverse=True)[:MAX_EXAMPLES]]
	return accounts, templates, examples


def _prompt(context, lines: list, accounts: dict, templates: dict, examples: list[str]) -> list[dict]:
	open_lines = "\n".join(
		f"{i}. net {flt(line.net):.2f}, VAT charged "
		f"{'unknown' if line.rate is None else f'{flt(line.rate):g} %'}, "
		f"description: {line.description or '-'}"
		for i, line in enumerate(lines)
	)
	invoice = "\n".join(
		filter(
			None,
			(
				f"Vendor: {context.vendor_name or context.supplier or '-'}",
				f"Vendor country: {context.vendor_country or 'unknown'}",
				context.vendor_tax_id and f"Vendor tax id: {context.vendor_tax_id}",
				context.text and f"Invoice text:\n{context.text[:MAX_TEXT_CHARS]}",
			),
		)
	)
	allowed_accounts = "\n".join(f"- {name} ({label})" for name, label in sorted(accounts.items()))
	allowed_templates = "\n".join(
		f"- {name} (charged rate {'?' if rate is None else f'{rate:g} %'})"
		for name, rate in sorted(templates.items())
	)
	return [
		{"role": "system", "content": INSTRUCTIONS.format(company=context.company)},
		{
			"role": "user",
			"content": f"ALLOWED ACCOUNTS\n{allowed_accounts}\n\nALLOWED TEMPLATES\n{allowed_templates}\n\n"
			f"BOOKING EXAMPLES (supplier | country | account | template | share | invoices)\n"
			+ "\n".join(examples)
			+ f"\n\nINVOICE\n{invoice}\n\nOPEN LINES\n{open_lines}",
		},
	]


def _ask(model_name: str, messages: list[dict]) -> dict | None:
	answer, usage = ask_json(model_name, messages, SCHEMA)
	frappe.local.epost_llm_usage = usage
	return answer
