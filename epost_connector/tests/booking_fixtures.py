"""Tax templates and booked invoices for the booking-suggestion tests.

Not a `test_*.py` module, so the runner does not collect it.

The templates mirror the two shapes that matter, not any country's real codes:
input tax added on top of the net ("charged 8.1 %"), and reverse charge, which
adds input tax and deducts the same amount as a liability ("charged 0 %").
"""

from __future__ import annotations

import frappe

from epost_connector.tests.site_base import TEST_COMPANY, TEST_COMPANY_ABBR, cost_center, ensure_supplier

INPUT_TAX = "Test Input Tax"
REVERSE_CHARGE = "Test Reverse Charge"
TEMPLATE_INPUT = "Test Input 8.1%"
TEMPLATE_REVERSE = "Test Reverse 8.1%"


def ensure_tax_setup(company: str) -> dict[str, str]:
	"""Two tax accounts and the matching Item and Purchase tax templates."""
	input_tax = _ensure_tax_account(company, INPUT_TAX)
	reverse = _ensure_tax_account(company, REVERSE_CHARGE)

	names = {
		"input": _ensure_templates(company, TEMPLATE_INPUT, [(input_tax, 8.1, "Add")]),
		"reverse": _ensure_templates(
			company, TEMPLATE_REVERSE, [(input_tax, 8.1, "Add"), (reverse, 8.1, "Deduct")]
		),
	}
	frappe.db.commit()
	return names


def expense_accounts(company: str, count: int = 3) -> list[str]:
	names = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": "Expense", "is_group": 0, "disabled": 0},
		pluck="name",
		order_by="name asc",
		limit=count,
	)
	if len(names) < count:
		raise AssertionError(f"{company} has fewer than {count} leaf expense accounts")
	return names


def book_invoice(
	company: str,
	supplier_name: str,
	lines: list[tuple[str, str | None, float]],
	posting_date: str = "2024-06-01",
	cost_center_name: str | None = None,
) -> str:
	"""A submitted Purchase Invoice with one line per (account, item tax template, net)."""
	supplier = ensure_supplier(supplier_name)
	invoice = frappe.new_doc("Purchase Invoice")
	invoice.update(
		{
			"company": company,
			"supplier": supplier,
			"set_posting_time": 1,
			"posting_date": posting_date,
			"bill_date": posting_date,
			"due_date": posting_date,
			"currency": frappe.get_cached_value("Company", company, "default_currency"),
			"conversion_rate": 1,
			"remarks": "booking fixture",
		}
	)
	for account, template, net in lines:
		invoice.append(
			"items",
			{
				"item_name": "Service",
				"description": "Service",
				"qty": 1,
				"rate": net,
				"uom": "Nos",
				"conversion_factor": 1,
				"expense_account": account,
				"item_tax_template": template,
				"cost_center": cost_center_name or cost_center(company),
			},
		)
	invoice.set_missing_values()
	invoice.insert(ignore_permissions=True)
	invoice.submit()
	frappe.db.commit()
	return invoice.name


def purge_booked(suppliers: list[str]) -> None:
	for name in frappe.get_all(
		"Purchase Invoice",
		filters={"supplier": ("in", suppliers), "remarks": "booking fixture"},
		pluck="name",
	):
		invoice = frappe.get_doc("Purchase Invoice", name)
		if invoice.docstatus == 1:
			invoice.cancel()
		frappe.delete_doc("Purchase Invoice", name, force=True, ignore_permissions=True)
		# Cancelling leaves the ledger rows behind, and deleting the last invoice
		# of a naming series hands its name to the next one. A draft created later
		# under that name would then appear to have posted.
		for doctype in ("GL Entry", "Payment Ledger Entry"):
			frappe.db.delete(doctype, {"voucher_type": "Purchase Invoice", "voucher_no": name})
	frappe.db.delete("ePost Rule", {"company": TEST_COMPANY})
	frappe.db.commit()


def _ensure_tax_account(company: str, account_name: str) -> str:
	name = f"{account_name} - {TEST_COMPANY_ABBR}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Liability", "is_group": 1, "account_type": "Tax"},
		"name",
	) or frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 1}, "name")
	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": company,
				"account_type": "Tax",
				"is_group": 0,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _ensure_templates(company: str, title: str, rows: list[tuple[str, float, str]]) -> str:
	"""An Item Tax Template and a Purchase Taxes and Charges Template of one name."""
	item_name = frappe.db.get_value("Item Tax Template", {"title": title, "company": company}, "name")
	if not item_name:
		item_name = (
			frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": title,
					"company": company,
					"taxes": [{"tax_type": account, "tax_rate": rate} for account, rate, _ in rows],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	if not frappe.db.exists("Purchase Taxes and Charges Template", item_name):
		template = frappe.get_doc(
			{
				"doctype": "Purchase Taxes and Charges Template",
				"title": title,
				"company": company,
				"taxes": [
					{
						"category": "Total",
						"add_deduct_tax": sign,
						"charge_type": "On Net Total",
						"account_head": account,
						"rate": rate,
						"description": account,
						"cost_center": cost_center(company),
					}
					for account, rate, sign in rows
				],
			}
		).insert(ignore_permissions=True)
		if template.name != item_name:
			# Both doctypes name themselves "<title> - <abbr>". The suggestion
			# relies on that to find the invoice-level template from the lines'.
			raise AssertionError(f"template names diverged: {template.name} != {item_name}")
	return item_name
