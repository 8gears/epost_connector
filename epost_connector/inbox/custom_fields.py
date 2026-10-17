"""Fields this app adds to Purchase Invoice, so review happens in ERPNext's own form.

Created on install and on every migrate; `create_custom_fields` updates a field
that already exists instead of duplicating it.
"""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {
	"Purchase Invoice": [
		{
			"fieldname": "epost_section",
			"fieldtype": "Section Break",
			"label": "ePost",
			"insert_after": "remarks",
			"collapsible": 0,
			"depends_on": "eval:doc.epost_letter",
		},
		{
			"fieldname": "epost_letter",
			"fieldtype": "Link",
			"label": "ePost Letter",
			"options": "ePost Letter",
			"insert_after": "epost_section",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "extraction_confidence",
			"fieldtype": "Percent",
			"label": "Extraction Confidence",
			"insert_after": "epost_letter",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "epost_column",
			"fieldtype": "Column Break",
			"insert_after": "extraction_confidence",
		},
		{
			"fieldname": "review_notes",
			"fieldtype": "Small Text",
			"label": "Review Notes",
			"insert_after": "epost_column",
			"read_only": 1,
			"no_copy": 1,
			"description": "What to check before submitting. Written when the draft was created from the letter.",
		},
	],
	"Purchase Invoice Item": [
		{
			"fieldname": "booking_source",
			"fieldtype": "Select",
			"label": "Booking Source",
			"options": "\nRule\nHistory\nLLM\nDefault\nNone",
			"insert_after": "expense_account",
			"read_only": 1,
			"no_copy": 1,
			"in_list_view": 0,
		},
		{
			"fieldname": "booking_confidence",
			"fieldtype": "Percent",
			"label": "Booking Confidence",
			"insert_after": "booking_source",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}


def install() -> None:
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
