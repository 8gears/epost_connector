"""Replay the booking suggestion over invoices that were already booked by hand.

Each submitted Purchase Invoice is suggested again from the rules plus only the
invoices posted *before* it, and every line is compared with how it was actually
booked. That makes the result a fair estimate of how the suggestion would have
done at the time, not a measure of how well it remembers the answer.

The VAT groups come from the booked lines themselves, so this measures the
booking step alone, as if extraction had read every invoice perfectly.

    bench --site <site> execute epost_connector.booking.backtest.run \
        --kwargs '{"company": "<company>", "exclude_invoices": ["..."]}'
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import frappe
from frappe.utils import flt

from epost_connector.booking import history
from epost_connector.booking.suggest import BookingContext, VatGroup, suggest


def run(
	company: str,
	exclude_invoices: Iterable[str] = (),
	exclude_account_prefixes: Iterable[str] = (),
	mixed_template: str | None = None,
	llm_model: str | None = None,
	max_misses: int = 50,
) -> dict:
	"""Score the suggestion against every submitted Purchase Invoice of `company`.

	`exclude_invoices` are neither scored nor used as history, for invoices known
	to be booked wrongly. Lines on an account starting with one of
	`exclude_account_prefixes` are not scored, but still count as history.

	With `llm_model`, lines left open are sent to that Flow Model, one call per
	invoice, with the invoice remarks as its only text. Real letters carry their
	whole text, so this understates what the model does on a letter.

	Lines at a rate the invoice booked more than one way cannot all match one
	suggestion for that rate. They are reported as `split_rate_lines` and left
	out of the totals.
	"""
	excluded = set(exclude_invoices)
	prefixes = tuple(exclude_account_prefixes)
	invoices = frappe.get_all(
		"Purchase Invoice",
		filters={"docstatus": 1, "company": company},
		fields=["name", "supplier", "posting_date", "remarks"],
		order_by="posting_date asc, name asc",
	)
	countries = dict(frappe.get_all("Supplier", fields=["name", "country"], as_list=True))

	totals = _Tally()
	by_source: dict[str, _Tally] = defaultdict(_Tally)
	misses: list[dict] = []
	invoices_scored = invoices_exact = 0
	tokens = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
	split = _Tally()

	for invoice in invoices:
		if invoice.name in excluded:
			continue
		lines = _booked_lines(invoice.name)
		scored = [
			line for line in lines if not (prefixes and (line.expense_account or "").startswith(prefixes))
		]
		if not scored:
			continue

		context = BookingContext(
			company=company,
			supplier=invoice.supplier,
			vendor_name=invoice.supplier,
			vendor_country=countries.get(invoice.supplier),
			text=invoice.remarks,
			groups=_groups(lines),
		)
		frappe.local.epost_llm_usage = None
		frappe.local.epost_llm_calls = 0
		suggestion = suggest(
			context,
			as_of=invoice.posting_date,
			exclude_invoices=excluded | {invoice.name},
			mixed_template=mixed_template,
			llm_model=llm_model,
		)
		tokens["calls"] += getattr(frappe.local, "epost_llm_calls", 0) or 0
		usage = getattr(frappe.local, "epost_llm_usage", None)
		if usage:
			tokens["prompt_tokens"] += usage.get("prompt_tokens") or 0
			tokens["completion_tokens"] += usage.get("completion_tokens") or 0
		by_rate = {_rate_key(line.rate): line for line in suggestion.lines}
		ambiguous = _split_rates(lines)

		invoices_scored += 1
		exact = True
		for line in scored:
			predicted = by_rate.get(_rate_key(history.charged_rate(line.item_tax_template)))
			account_ok = bool(predicted and predicted.expense_account == line.expense_account)
			template_ok = bool(predicted and predicted.item_tax_template == line.item_tax_template)
			source = predicted.account_source if predicted else "None"
			weight = abs(flt(line.base_net_amount))

			if _rate_key(history.charged_rate(line.item_tax_template)) in ambiguous:
				# One suggestion per rate cannot match lines that were booked to
				# different accounts at that rate, so they are counted apart.
				split.add(
					account_ok, template_ok, weight, suggested=bool(predicted and predicted.expense_account)
				)
				continue
			for tally in (totals, by_source[source]):
				tally.add(
					account_ok, template_ok, weight, suggested=bool(predicted and predicted.expense_account)
				)
			if not (account_ok and template_ok):
				exact = False
				if len(misses) < max_misses:
					misses.append(
						{
							"invoice": invoice.name,
							"supplier": invoice.supplier,
							"booked": [line.expense_account, line.item_tax_template],
							"suggested": [
								predicted.expense_account if predicted else None,
								predicted.item_tax_template if predicted else None,
							],
							"source": source,
						}
					)
		invoices_exact += exact

	return {
		"company": company,
		"invoices_scored": invoices_scored,
		"invoices_exact": invoices_exact,
		"lines": totals.as_dict(),
		"by_source": {source: tally.as_dict() for source, tally in sorted(by_source.items())},
		"llm_usage": tokens,
		"split_rate_lines": split.as_dict(),
		"misses": misses,
	}


class _Tally:
	def __init__(self) -> None:
		self.lines = self.suggested = self.account = self.template = 0
		self.weight = self.account_weight = self.template_weight = 0.0

	def add(self, account_ok: bool, template_ok: bool, weight: float, suggested: bool) -> None:
		self.lines += 1
		self.suggested += suggested
		self.account += account_ok
		self.template += template_ok
		self.weight += weight
		self.account_weight += weight if account_ok else 0.0
		self.template_weight += weight if template_ok else 0.0

	def as_dict(self) -> dict:
		def ratio(part: float, whole: float) -> float:
			return round(part / whole, 4) if whole else 0.0

		return {
			"lines": self.lines,
			"suggested": self.suggested,
			"account_accuracy": ratio(self.account, self.lines),
			"template_accuracy": ratio(self.template, self.lines),
			"account_accuracy_by_amount": ratio(self.account_weight, self.weight),
			"template_accuracy_by_amount": ratio(self.template_weight, self.weight),
		}


def _booked_lines(invoice: str) -> list:
	return frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": invoice, "parenttype": "Purchase Invoice"},
		fields=["expense_account", "item_tax_template", "base_net_amount", "net_amount", "description"],
		order_by="idx asc",
	)


def _groups(lines: list) -> list[VatGroup]:
	"""One VAT group per rate the booked lines were charged at, as extraction would read it."""
	groups: dict[float | None, VatGroup] = {}
	for line in lines:
		rate = history.charged_rate(line.item_tax_template)
		group = groups.setdefault(_rate_key(rate), VatGroup(net=0.0, rate=rate, description=line.description))
		# Invoice currency, as extraction reads it off the letter.
		group.net += flt(line.net_amount)
	return list(groups.values())


def _split_rates(lines: list) -> set:
	"""Rates at which the invoice's lines were booked more than one way."""
	seen: dict = {}
	for line in lines:
		key = _rate_key(history.charged_rate(line.item_tax_template))
		seen.setdefault(key, set()).add((line.expense_account, line.item_tax_template))
	return {key for key, ways in seen.items() if len(ways) > 1}


def _rate_key(rate: float | None) -> float | None:
	return None if rate is None else round(flt(rate), 2)
