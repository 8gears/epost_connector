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
from frappe.utils import flt, getdate, nowdate

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

	rows = answer.get("lines")
	answered: set[int] = set()
	for row in rows if isinstance(rows, list) else []:
		index = _index(row, len(lines))
		if index is None or index in answered:
			continue
		account, template = row.get("expense_account"), row.get("item_tax_template")
		if not isinstance(account, str) or account not in accounts:
			continue
		answered.add(index)
		line = lines[index]
		line.expense_account = account
		line.account_source = "LLM"
		# A template the company uses can still be wrong for this line's rate, so
		# it is kept only when it matches; the account stands either way.
		if (
			isinstance(template, str)
			and template in templates
			and not line.item_tax_template
			and history.rate_fits(template, line.rate)
		):
			line.item_tax_template = template
			line.template_source = "LLM"
		if row.get("reason"):
			line.evidence.append(f"LLM: {str(row['reason'])[:200]}")
		yield line, min(max(flt(row.get("confidence")), 0.0), MAX_CONFIDENCE)


def _index(row, count: int) -> int | None:
	"""The line index a row answers, or None. `True` is not an index, `1.0` is."""
	if not isinstance(row, dict):
		return None
	value = row.get("index")
	if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
		return None
	index = int(value)
	return index if 0 <= index < count else None


def _choices(company: str, as_of, excluded: set[str]) -> tuple[dict, dict, list[str]]:
	"""Accounts and templates in use, and one booking example per supplier."""
	lines = history.past_lines(None, company, as_of, excluded)
	if not lines:
		return {}, {}, []

	by_supplier: dict[str, list[history.PastLine]] = {}
	for line in lines:
		by_supplier.setdefault(line.supplier, []).append(line)

	accounts = dict(
		frappe.get_all(
			"Account",
			filters={"name": ("in", list({line.expense_account for line in lines})), "is_group": 0},
			fields=["name", "account_name"],
			as_list=True,
		)
	)
	templates = {
		t: history.charged_rate(t)
		for t in {line.item_tax_template for line in lines if line.item_tax_template}
	}
	countries = dict(
		frappe.get_all(
			"Supplier", filters={"name": ("in", list(by_supplier))}, fields=["name", "country"], as_list=True
		)
	)
	weighted = []
	for supplier, past in by_supplier.items():
		ranked = history.rank(past, as_of or nowdate())
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
	# Read by the backtest, which counts calls apart from tokens: a provider
	# may answer without reporting usage.
	frappe.local.epost_llm_calls = (getattr(frappe.local, "epost_llm_calls", None) or 0) + 1
	frappe.local.epost_llm_usage = usage
	return answer
