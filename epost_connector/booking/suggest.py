"""Suggest how a supplier invoice is booked: account, VAT template, cost center.

Three sources, asked in a fixed order, each filling only what is still open:

1. `ePost Rule` rows applied *before* history. For what a human wants
   decided explicitly, whatever the history says.
2. The supplier's own history (`history.py`), when one combination clearly wins.
3. Rules applied *after* history. Defaults for suppliers with no history, such
   as "a foreign supplier on a 4xxx account gets the reverse-charge template".

The LLM fallback is a separate, optional step (`llm.py`) for what is still open.
Nothing here books anything: the result is a suggestion a human confirms on a
draft invoice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date

import frappe
from frappe.utils import flt, nowdate

from epost_connector.booking import history
from epost_connector.inbox.matching import tax_key

RULE_DOCTYPE = "ePost Rule"
BEFORE_HISTORY = "Before history"
AFTER_HISTORY = "After history"
VAT_CHARGED = "Charged"
VAT_NOT_CHARGED = "Not charged"

#: Confidence per source. History carries its own share instead.
RULE_CONFIDENCE = 1.0
DEFAULT_CONFIDENCE = 0.3

SOURCE_RULE = "Rule"
SOURCE_HISTORY = "History"
SOURCE_LLM = "LLM"
SOURCE_DEFAULT = "Default"
SOURCE_NONE = "None"


@dataclass
class VatGroup:
	"""The part of an invoice taxed at one rate. `rate` is None when unknown."""

	net: float
	rate: float | None = None
	description: str | None = None


@dataclass
class BookingContext:
	company: str
	supplier: str | None = None
	vendor_name: str | None = None
	vendor_tax_id: str | None = None
	vendor_country: str | None = None
	document_kind: str | None = None
	text: str | None = None
	groups: list[VatGroup] = field(default_factory=list)


@dataclass
class SuggestedLine:
	net: float
	rate: float | None = None
	description: str | None = None
	expense_account: str | None = None
	item_tax_template: str | None = None
	cost_center: str | None = None
	account_source: str = SOURCE_NONE
	template_source: str = SOURCE_NONE
	confidence: float = 0.0
	evidence: list[str] = field(default_factory=list)


@dataclass
class BookingSuggestion:
	lines: list[SuggestedLine]
	taxes_and_charges: str | None = None

	@property
	def source(self) -> str:
		"""The weakest account source across the lines, for a list-view filter."""
		order = [SOURCE_NONE, SOURCE_DEFAULT, SOURCE_LLM, SOURCE_HISTORY, SOURCE_RULE]
		if not self.lines:
			return SOURCE_NONE
		return min((line.account_source for line in self.lines), key=order.index)

	def as_dict(self) -> dict:
		return {"lines": [asdict(line) for line in self.lines], "taxes_and_charges": self.taxes_and_charges}


def suggest(
	context: BookingContext,
	as_of: date | str | None = None,
	exclude_invoices: Iterable[str] = (),
	mixed_template: str | None = None,
	llm_model: str | None = None,
) -> BookingSuggestion:
	"""Suggest one line per VAT group of `context`.

	`as_of` and `exclude_invoices` limit the history to what was known at a point
	in time, which is what makes a backtest over past invoices honest. The LLM is
	asked only when `llm_model` names a Flow Model, and only about lines that
	rules and history left without an account.
	"""
	rules = _load_rules(context.company)
	before = [r for r in rules if r.apply_when == BEFORE_HISTORY]
	after = [r for r in rules if r.apply_when == AFTER_HISTORY]
	ranked: list[history.Combination] = []
	if context.supplier:
		ranked = history.rank(
			history.past_lines(context.supplier, context.company, as_of, exclude_invoices),
			as_of or nowdate(),
		)

	groups = context.groups or [VatGroup(net=0.0)]
	lines = [SuggestedLine(net=flt(g.net), rate=g.rate, description=g.description) for g in groups]
	confidences: dict[int, list[float]] = {id(line): [] for line in lines}

	for line in lines:
		_apply_rules(line, context, before, confidences[id(line)])
		_apply_history(line, ranked, confidences[id(line)])

	open_lines = [line for line in lines if not line.expense_account]
	if llm_model and open_lines:
		from epost_connector.booking import llm

		for line, confidence in llm.fill(context, open_lines, llm_model, as_of, exclude_invoices):
			confidences[id(line)].append(confidence)

	for line in lines:
		_apply_rules(line, context, after, confidences[id(line)], over_llm=True)
		found = confidences[id(line)]
		line.confidence = round(min(found), 4) if found and line.expense_account else 0.0

	return BookingSuggestion(lines=lines, taxes_and_charges=_taxes_template(lines, mixed_template))


@dataclass
class Route:
	status: str | None = None
	supplier: str | None = None
	rule: str | None = None


def route(context: BookingContext) -> Route:
	"""What the rules decide about the letter as a whole, before any line is booked.

	The first matching rule with `set_status` stops the letter; the first with
	`set_supplier` names its supplier. Rules without either are booking rules and
	are left to `suggest`.
	"""
	decided = Route()
	letter_level = SuggestedLine(net=0.0)
	for rule in _load_rules(context.company):
		if not (rule.set_status or rule.set_supplier) or not _rule_matches(rule, context, letter_level):
			continue
		if rule.set_status and not decided.status:
			decided.status, decided.rule = rule.set_status, rule.name
			return decided
		if rule.set_supplier and not decided.supplier:
			decided.supplier = rule.set_supplier
			decided.rule = decided.rule or rule.name
	return decided


def _apply_rules(
	line: SuggestedLine, context: BookingContext, rules: list, confidences: list, over_llm: bool = False
) -> None:
	"""Fill what is still open from the first matching rules.

	With `over_llm`, a rule may also replace a VAT template the LLM chose: the
	rules after history encode how VAT is coded, which is a decision a human
	wrote down and the model only guesses at.
	"""
	for rule in rules:
		template_open = not line.item_tax_template or (over_llm and line.template_source == SOURCE_LLM)
		if line.expense_account and not template_open and line.cost_center:
			return
		if not _rule_matches(rule, context, line):
			continue
		# A rule after history is a default, not a decision about this supplier.
		source = SOURCE_RULE if rule.apply_when == BEFORE_HISTORY else SOURCE_DEFAULT
		filled = False
		if not line.expense_account and rule.expense_account:
			line.expense_account = rule.expense_account
			line.account_source = source
			filled = True
		if template_open and rule.item_tax_template and history.rate_fits(rule.item_tax_template, line.rate):
			line.item_tax_template = rule.item_tax_template
			line.template_source = source
			filled = True
		if not line.cost_center and rule.cost_center:
			line.cost_center = rule.cost_center
		if filled:
			line.evidence.append(rule.name)
			confidences.append(RULE_CONFIDENCE if rule.apply_when == BEFORE_HISTORY else DEFAULT_CONFIDENCE)


def _apply_history(line: SuggestedLine, ranked: list[history.Combination], confidences: list) -> None:
	if line.expense_account and line.item_tax_template:
		return
	fitting = [
		c
		for c in ranked
		if history.rate_fits(c.item_tax_template, line.rate)
		and (not line.item_tax_template or c.item_tax_template == line.item_tax_template)
	]
	if line.expense_account:
		_apply_history_template(line, fitting, confidences)
		return

	total = sum(c.weight for c in fitting)
	if not total:
		return
	top = fitting[0]
	share = top.weight / total
	if share < history.MIN_SHARE:
		return

	line.expense_account = top.expense_account
	line.account_source = SOURCE_HISTORY
	if not line.item_tax_template and top.item_tax_template:
		line.item_tax_template = top.item_tax_template
		line.template_source = SOURCE_HISTORY
	if not line.cost_center:
		line.cost_center = top.cost_center
	line.evidence.extend(top.invoices[:5])
	confidences.append(share)


def _apply_history_template(
	line: SuggestedLine, fitting: list[history.Combination], confidences: list
) -> None:
	"""A rule chose the account; the supplier's VAT treatment still comes from its history.

	How a supplier is taxed does not depend on which account an invoice is booked
	to, so the lines on that account are preferred and all lines are the fallback.
	"""
	pool = [c for c in fitting if c.expense_account == line.expense_account] or fitting
	weights: dict[str, float] = {}
	for combo in pool:
		if combo.item_tax_template:
			weights[combo.item_tax_template] = weights.get(combo.item_tax_template, 0.0) + combo.weight
	total = sum(weights.values())
	if not total:
		return
	template, weight = max(weights.items(), key=lambda item: item[1])
	if weight / total < history.MIN_SHARE:
		return
	line.item_tax_template = template
	line.template_source = SOURCE_HISTORY
	confidences.append(weight / total)


def _rule_matches(rule, context: BookingContext, line: SuggestedLine) -> bool:
	if rule.supplier and rule.supplier != context.supplier:
		return False
	if rule.document_kind and rule.document_kind != context.document_kind:
		return False
	if rule.vendor_tax_id and tax_key(rule.vendor_tax_id) != tax_key(context.vendor_tax_id):
		return False
	if rule.vendor_country and rule.vendor_country != context.vendor_country:
		return False
	if rule.foreign_only or rule.domestic_only:
		home = frappe.get_cached_value("Company", context.company, "country")
		if not context.vendor_country:
			return False
		foreign = context.vendor_country != home
		if (rule.foreign_only and not foreign) or (rule.domestic_only and foreign):
			return False
	if rule.vat_on_invoice == VAT_CHARGED and not (line.rate and line.rate > 0):
		return False
	if rule.vat_on_invoice == VAT_NOT_CHARGED and (line.rate is None or line.rate > 0):
		return False
	if rule.account_prefix:
		prefixes = [p.strip() for p in rule.account_prefix.split(",") if p.strip()]
		if not (line.expense_account or "").startswith(tuple(prefixes)):
			return False
	if rule.keyword:
		haystack = "\n".join(filter(None, (context.vendor_name, line.description, context.text)))
		try:
			if not re.search(rule.keyword, haystack, re.IGNORECASE):
				return False
		except re.error:
			return False
	return True


def _taxes_template(lines: list[SuggestedLine], mixed_template: str | None) -> str | None:
	"""The invoice-level template: the lines' own when they agree, else the mixed one.

	Relies on a Purchase Taxes and Charges Template named like the Item Tax
	Template, which is how the per-line templates resolve their accounts.
	"""
	templates = {line.item_tax_template for line in lines}
	if None in templates or not templates:
		return None
	if len(templates) == 1:
		(name,) = templates
		return name if frappe.db.exists("Purchase Taxes and Charges Template", name) else None
	return mixed_template


def _load_rules(company: str) -> list:
	if not frappe.db.table_exists(RULE_DOCTYPE):
		return []
	return frappe.get_all(
		RULE_DOCTYPE,
		filters={"enabled": 1, "company": company},
		fields=[
			"name",
			"apply_when",
			"supplier",
			"vendor_tax_id",
			"vendor_country",
			"foreign_only",
			"domestic_only",
			"vat_on_invoice",
			"document_kind",
			"set_status",
			"set_supplier",
			"account_prefix",
			"keyword",
			"expense_account",
			"item_tax_template",
			"cost_center",
		],
		order_by="priority asc, name asc",
	)
