"""Copy ePost Booking Rule rows into ePost Rule, keeping their names, then drop the old DocType."""

from __future__ import annotations

import frappe

FIELDS = (
	"title",
	"enabled",
	"priority",
	"apply_when",
	"company",
	"supplier",
	"vendor_tax_id",
	"keyword",
	"vendor_country",
	"foreign_only",
	"domestic_only",
	"vat_on_invoice",
	"account_prefix",
	"expense_account",
	"item_tax_template",
	"cost_center",
	"notes",
)


def execute() -> None:
	if not frappe.db.table_exists("ePost Booking Rule"):
		return
	present = [f for f in FIELDS if frappe.db.has_column("ePost Booking Rule", f)]
	for row in frappe.db.sql(
		f"select name, {', '.join(f'`{f}`' for f in present)} from `tabePost Booking Rule`", as_dict=True
	):
		if frappe.db.exists("ePost Rule", row.name):
			continue
		doc = frappe.get_doc({"doctype": "ePost Rule", **{f: row.get(f) for f in present}})
		doc.insert(ignore_permissions=True, set_name=row.name)
	frappe.delete_doc("DocType", "ePost Booking Rule", force=True, ignore_missing=True)
