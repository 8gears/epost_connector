"""Move what extraction wrote onto ePost Letter into ePost Extraction Log rows.

Letters stay what ePost delivered; the extracted values now live in a log. The
old columns are still in the table after the fields left the DocType, so they
are read with SQL. No model is called: the values are copied, not re-read.
"""

from __future__ import annotations

import json

import frappe

OLD_COLUMNS = (
	"document_kind",
	"vendor_name",
	"vendor_tax_id",
	"vendor_country",
	"iban",
	"qr_reference",
	"invoice_number",
	"invoice_date",
	"due_date",
	"currency",
	"net_amount",
	"amount",
	"vat_amount",
	"extraction_confidence",
	"extraction_raw",
)


def execute() -> None:
	if not frappe.db.has_column("ePost Letter", "extraction_raw"):
		return
	present = [c for c in OLD_COLUMNS if frappe.db.has_column("ePost Letter", c)]
	rows = frappe.db.sql(
		f"""select name, {", ".join(f"`{c}`" for c in present)} from `tabePost Letter`
		where coalesce(extraction_raw, '') not in ('', '{{}}') and coalesce(extraction_log, '') = ''""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value("ePost Letter", row.name, "extraction_log", _log_for(row), update_modified=False)


def _log_for(row) -> str:
	try:
		raw = json.loads(row.get("extraction_raw") or "{}")
	except ValueError:
		raw = {}
	answer = raw.get("answer") if isinstance(raw.get("answer"), dict) else {}
	usage = raw.get("usage") or {}
	log = frappe.get_doc(
		{
			"doctype": "ePost Extraction Log",
			"letter": row.name,
			"kind": "Extraction",
			"status": "Success",
			"model": raw.get("model"),
			"input_type": raw.get("input"),
			"prompt_tokens": usage.get("prompt_tokens") or 0,
			"completion_tokens": usage.get("completion_tokens") or 0,
			"answer": json.dumps(
				{
					"answer": answer,
					"checks": raw.get("checks"),
					"vat_breakdown": answer.get("vat_breakdown"),
					"line_items": answer.get("line_items"),
					"summary": answer.get("summary"),
				},
				indent=1,
				default=str,
			),
			"document_kind": row.get("document_kind"),
			"vendor_name": row.get("vendor_name"),
			"vendor_tax_id": row.get("vendor_tax_id"),
			"vendor_country": row.get("vendor_country"),
			"vendor_address": answer.get("vendor_address"),
			"iban": row.get("iban"),
			"qr_reference": row.get("qr_reference"),
			"invoice_number": row.get("invoice_number"),
			"invoice_date": row.get("invoice_date"),
			"due_date": row.get("due_date"),
			"currency": row.get("currency"),
			"net_amount": row.get("net_amount"),
			"vat_amount": row.get("vat_amount"),
			"gross_amount": row.get("amount"),
			"confidence": row.get("extraction_confidence"),
		}
	)
	log.flags.ignore_links = True
	log.insert(ignore_permissions=True)
	return log.name
