"""Booking memory: how a supplier's past invoices were booked.

This is an exact lookup, not a similarity search. The question "which account and
VAT template did this supplier's invoices go to?" has one answer per past line, and
submitted Purchase Invoices hold every one of them. A vector search over the same
rows would return text passages rather than an account split, and could miss rows
that a query never misses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import frappe
from frappe.utils import flt, getdate

#: Days after which a past booking counts half. Recent bookings win a tie because
#: an accountant who changed their mind about a supplier did so for a reason.
HALF_LIFE_DAYS = 365

#: Share of the weighted history the leading combination must hold to be used.
#: Below it the history is split three or more ways and says little. A backtest
#: over 595 hand-booked lines scored 94.6 % at a 0.9 cut and 93.2 % at 0.5 while
#: covering 71 more lines, and every suggestion is reviewed on a draft anyway, so
#: the cut is low and the share is reported as the confidence instead.
MIN_SHARE = 0.5


@dataclass(frozen=True)
class PastLine:
	invoice: str
	supplier: str
	posting_date: date
	expense_account: str
	item_tax_template: str | None
	cost_center: str | None
	amount: float


@dataclass
class Combination:
	"""One way the supplier was booked: an account and a VAT template.

	The cost center is not part of the key. A supplier booked to one account and
	template across several cost centers is one consistent booking, and splitting
	it by center would push each part under `MIN_SHARE`. The heaviest center is
	suggested instead.
	"""

	expense_account: str
	item_tax_template: str | None
	cost_center: str | None = None
	weight: float = 0.0
	share: float = 0.0
	invoices: list[str] = field(default_factory=list)


def past_lines(
	supplier: str | None,
	company: str,
	before: date | str | None = None,
	exclude_invoices: Iterable[str] = (),
) -> list[PastLine]:
	"""Submitted Purchase Invoice lines of `supplier` (every supplier when None).

	Lines on an account that has since been disabled are left out: ERPNext would
	refuse that account on the draft the suggestion ends up on.
	"""
	invoice = frappe.qb.DocType("Purchase Invoice")
	item = frappe.qb.DocType("Purchase Invoice Item")
	account = frappe.qb.DocType("Account")
	query = (
		frappe.qb.from_(item)
		.join(invoice)
		.on(invoice.name == item.parent)
		.join(account)
		.on(account.name == item.expense_account)
		.select(
			invoice.name,
			invoice.supplier,
			invoice.posting_date,
			item.expense_account,
			item.item_tax_template,
			item.cost_center,
			item.base_net_amount,
		)
		.where(
			(invoice.docstatus == 1)
			& (invoice.company == company)
			& (item.parenttype == "Purchase Invoice")
			& (account.disabled == 0)
		)
	)
	if supplier:
		query = query.where(invoice.supplier == supplier)
	if before:
		query = query.where(invoice.posting_date < getdate(before))
	excluded = list(set(exclude_invoices))
	if excluded:
		query = query.where(invoice.name.notin(excluded))

	return [
		PastLine(
			invoice=row.name,
			supplier=row.supplier,
			posting_date=getdate(row.posting_date),
			expense_account=row.expense_account,
			item_tax_template=row.item_tax_template or None,
			cost_center=row.cost_center or None,
			amount=flt(row.base_net_amount),
		)
		for row in query.run(as_dict=True)
	]


def rank(lines: Iterable[PastLine], as_of: date | str | None = None) -> list[Combination]:
	"""Group past lines by account and VAT template, heaviest first.

	A line weighs its absolute amount, halved for every `HALF_LIFE_DAYS` of age.
	Absolute, because a credit note booked to an account is still evidence that
	the account is where this supplier goes.
	"""
	as_of = getdate(as_of) if as_of else None
	combos: dict[tuple, Combination] = {}
	centers: dict[tuple, dict[str | None, float]] = {}
	for line in lines:
		key = (line.expense_account, line.item_tax_template)
		combo = combos.setdefault(key, Combination(*key))
		weight = abs(line.amount) * _decay(line.posting_date, as_of)
		combo.weight += weight
		by_center = centers.setdefault(key, {})
		by_center[line.cost_center] = by_center.get(line.cost_center, 0.0) + weight
		if line.invoice not in combo.invoices:
			combo.invoices.append(line.invoice)

	total = sum(c.weight for c in combos.values())
	ranked = sorted(combos.values(), key=lambda c: c.weight, reverse=True)
	for combo in ranked:
		combo.share = combo.weight / total if total else 0.0
		weights = centers[(combo.expense_account, combo.item_tax_template)]
		combo.cost_center = max(weights, key=weights.get)
	return ranked


def charged_rate(item_tax_template: str | None) -> float | None:
	"""The VAT rate a supplier invoice shows when booked with this template.

	Read from the Purchase Taxes and Charges Template of the same name, adding
	`Add` rows and subtracting `Deduct` rows. A reverse-charge template adds input
	tax and deducts the same amount as a liability, so it nets to 0 %: the supplier
	charged no VAT, which is exactly what the invoice shows. The largest Item Tax
	Template rate would say 8.1 % instead and pair a reverse-charge booking with an
	invoice that carries Swiss VAT.

	None when no Purchase Taxes and Charges Template carries that name, because
	then the rate cannot be told apart from the sign.
	"""
	if not item_tax_template:
		return None
	cache = getattr(frappe.local, "epost_charged_rates", None)
	if cache is None:
		cache = frappe.local.epost_charged_rates = {}
	if item_tax_template not in cache:
		rows = frappe.get_all(
			"Purchase Taxes and Charges",
			filters={"parent": item_tax_template, "parenttype": "Purchase Taxes and Charges Template"},
			fields=["rate", "add_deduct_tax", "charge_type"],
		)
		rows = [r for r in rows if r.charge_type == "On Net Total"]
		cache[item_tax_template] = (
			round(sum(flt(r.rate) * (-1 if r.add_deduct_tax == "Deduct" else 1) for r in rows), 4)
			if rows
			else None
		)
	return cache[item_tax_template]


#: How far a rate read off a letter may be from a template's rate and still match.
RATE_TOLERANCE = 0.05


def rate_fits(item_tax_template: str | None, rate: float | None) -> bool:
	"""Whether `item_tax_template` books a line on which the supplier charged `rate`."""
	if rate is None or not item_tax_template:
		return True
	template_rate = charged_rate(item_tax_template)
	return template_rate is not None and abs(template_rate - flt(rate)) <= RATE_TOLERANCE


def _decay(posted: date, as_of: date | None) -> float:
	if not as_of:
		return 1.0
	age = max((as_of - posted).days, 0)
	return 0.5 ** (age / HALF_LIFE_DAYS)
