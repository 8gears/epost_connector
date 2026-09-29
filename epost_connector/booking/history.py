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
	posting_date: date
	expense_account: str
	item_tax_template: str | None
	cost_center: str | None
	amount: float


@dataclass
class Combination:
	"""One way the supplier was booked, with the weight of the lines behind it."""

	expense_account: str
	item_tax_template: str | None
	cost_center: str | None
	weight: float = 0.0
	share: float = 0.0
	invoices: list[str] = field(default_factory=list)


def past_lines(
	supplier: str,
	company: str,
	before: date | str | None = None,
	exclude_invoices: Iterable[str] = (),
) -> list[PastLine]:
	"""Submitted Purchase Invoice lines of `supplier`, optionally only those before a date."""
	filters = {"docstatus": 1, "supplier": supplier, "company": company}
	if before:
		filters["posting_date"] = ("<", getdate(before))
	excluded = set(exclude_invoices)

	invoices = frappe.get_all("Purchase Invoice", filters=filters, fields=["name", "posting_date"])
	dates = {inv.name: inv.posting_date for inv in invoices if inv.name not in excluded}
	if not dates:
		return []

	rows = frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": ("in", list(dates)), "parenttype": "Purchase Invoice"},
		fields=["parent", "expense_account", "item_tax_template", "cost_center", "base_net_amount"],
	)
	return [
		PastLine(
			invoice=row.parent,
			posting_date=getdate(dates[row.parent]),
			expense_account=row.expense_account,
			item_tax_template=row.item_tax_template or None,
			cost_center=row.cost_center or None,
			amount=flt(row.base_net_amount),
		)
		for row in rows
		if row.expense_account
	]


def rank(lines: Iterable[PastLine], as_of: date | str | None = None) -> list[Combination]:
	"""Group past lines by how they were booked, heaviest first.

	A line weighs its absolute amount, halved for every `HALF_LIFE_DAYS` of age.
	Absolute, because a credit note booked to an account is still evidence that
	the account is where this supplier goes.
	"""
	as_of = getdate(as_of) if as_of else None
	combos: dict[tuple, Combination] = {}
	for line in lines:
		key = (line.expense_account, line.item_tax_template, line.cost_center)
		combo = combos.setdefault(key, Combination(*key))
		combo.weight += abs(line.amount) * _decay(line.posting_date, as_of)
		if line.invoice not in combo.invoices:
			combo.invoices.append(line.invoice)

	total = sum(c.weight for c in combos.values())
	ranked = sorted(combos.values(), key=lambda c: c.weight, reverse=True)
	for combo in ranked:
		combo.share = combo.weight / total if total else 0.0
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


def _decay(posted: date, as_of: date | None) -> float:
	if not as_of:
		return 1.0
	age = max((as_of - posted).days, 0)
	return 0.5 ** (age / HALF_LIFE_DAYS)
