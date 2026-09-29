# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostBookingRule` and cannot be renamed to PascalCase.

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document


class ePostBookingRule(Document):
	def validate(self) -> None:
		# Frappe checks mandatory fields after `validate`, so without this the
		# first target check would report "belongs to X, not None".
		if not self.company:
			frappe.throw(_("Set a Company: accounts, VAT templates and cost centers belong to one"))

		if self.foreign_only and self.domestic_only:
			frappe.throw(_("A rule cannot be both foreign-only and domestic-only"))

		if not (self.expense_account or self.item_tax_template or self.cost_center):
			frappe.throw(_("Set at least one of Expense Account, Item Tax Template or Cost Center"))

		if self.keyword:
			try:
				re.compile(self.keyword)
			except re.error as exc:
				frappe.throw(_("Keyword is not a valid regular expression: {0}").format(exc))

		self._validate_account("expense_account")
		self._validate_cost_center()
		self._validate_item_tax_template()

	def _validate_account(self, fieldname: str) -> None:
		account = self.get(fieldname)
		if not account:
			return
		row = frappe.db.get_value("Account", account, ["is_group", "company", "disabled"], as_dict=True)
		if row.is_group:
			frappe.throw(_("{0} is a group account; pick a ledger account").format(account))
		if row.disabled:
			frappe.throw(_("{0} is disabled").format(account))
		if row.company != self.company:
			frappe.throw(_("{0} belongs to {1}, not {2}").format(account, row.company, self.company))

	def _validate_cost_center(self) -> None:
		if not self.cost_center:
			return
		row = frappe.db.get_value("Cost Center", self.cost_center, ["is_group", "company"], as_dict=True)
		if row.is_group:
			frappe.throw(_("{0} is a group cost center").format(self.cost_center))
		if row.company != self.company:
			frappe.throw(_("{0} belongs to {1}, not {2}").format(self.cost_center, row.company, self.company))

	def _validate_item_tax_template(self) -> None:
		if not self.item_tax_template:
			return
		company = frappe.db.get_value("Item Tax Template", self.item_tax_template, "company")
		if company and company != self.company:
			frappe.throw(
				_("{0} belongs to {1}, not {2}").format(self.item_tax_template, company, self.company)
			)
