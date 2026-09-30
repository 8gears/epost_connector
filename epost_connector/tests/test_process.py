"""The letter pipeline, end to end: extract, route, supplier, duplicate, draft.

The extractor is registered inside the test and returns a fixed result, so what
is tested is where each kind of letter ends up and what it leaves behind.
"""

from __future__ import annotations

import dataclasses
import datetime
from unittest.mock import patch

import frappe

from epost_connector.epost.sync import sync_letters
from epost_connector.extraction.base import ExtractionResult, LetterExtractor
from epost_connector.inbox.process import process_letter, process_letter_by_name
from epost_connector.tests.site_base import (
	cost_center,
	ensure_company,
	ensure_fiscal_years,
	ensure_supplier,
	ePostSiteTestCase,
	expense_account,
	registered_extractor,
)

KNOWN = "Pipeline Known Supplier AG"


def _returning(result: ExtractionResult, same_number: bool = False):
	"""Every letter reads as `result`, numbered by its letter id unless `same_number`.

	The mock letterbox holds several letters and the sync order is not the
	test's to choose, so distinct numbers keep them from being duplicates of
	each other unless a test wants exactly that.
	"""

	class Fixed(LetterExtractor):
		name = "PipelineTest"

		def extract(self, letter_doc, pdf_bytes):
			if same_number:
				return result
			return dataclasses.replace(result, invoice_number=letter_doc.letter_id)

	return registered_extractor("PipelineTest", Fixed)


def _invoice(**values) -> ExtractionResult:
	base = dict(
		document_kind="Invoice",
		vendor_name=KNOWN,
		invoice_number="P-100",
		invoice_date="2024-05-02",
		currency="CHF",
		net_amount=100.0,
		vat_amount=8.1,
		gross_amount=108.1,
		confidence=0.9,
	)
	return ExtractionResult(**{**base, **values})


class ProcessTestCase(ePostSiteTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()
		self.company = ensure_company()
		ensure_fiscal_years(2024, datetime.date.today().year)
		self.supplier = ensure_supplier(KNOWN)
		self.configure_settings(
			company=self.company,
			default_expense_account=expense_account(self.company),
			default_cost_center=cost_center(self.company),
		)
		frappe.db.delete("ePost Rule", {"company": self.company})
		frappe.db.delete("Supplier Alias", {"supplier": self.supplier})
		frappe.db.commit()


class ProcessTest(ProcessTestCase):
	def test_a_known_supplier_goes_straight_to_a_draft(self):
		with _returning(_invoice()):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Drafted")
		self.assertEqual(doc.supplier, self.supplier)
		invoice = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		self.assertEqual(invoice.docstatus, 0)
		self.assertEqual(invoice.bill_no, "inbox-1")
		self.assertEqual(invoice.epost_letter, doc.name)

	def test_an_unknown_supplier_waits_and_continues_once_mapped(self):
		with _returning(_invoice(vendor_name="Brand New Vendor GmbH")):
			sync_letters()

			doc = self.letter_doc("inbox-1")
			self.assertEqual(doc.status, "Waiting for Supplier")
			self.assertIn("Brand New Vendor GmbH", doc.processing_note)

			with patch("frappe.enqueue") as enqueue:
				doc.supplier = self.supplier
				doc.save(ignore_permissions=True)
			self.assertEqual(enqueue.call_args.kwargs["letter_name"], doc.name)

			process_letter_by_name(doc.name)

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Drafted")
		self.assertTrue(doc.purchase_invoice)
		alias = frappe.db.get_value("Supplier Alias", {"supplier": self.supplier}, "alias_name")
		self.assertEqual(alias, "Brand New Vendor GmbH", "the mapping is remembered")

	def test_an_invoice_already_recorded_is_a_duplicate_not_a_second_draft(self):
		with _returning(_invoice(), same_number=True):
			sync_letters()

		letters = frappe.get_all(
			"ePost Letter",
			filters={"extraction_log": ("is", "set")},
			fields=["status", "purchase_invoice", "processing_note"],
		)
		drafted = [row for row in letters if row.status == "Drafted"]
		self.assertEqual(len(drafted), 1)
		self.assertEqual(frappe.db.count("Purchase Invoice", {"bill_no": "P-100"}), 1)
		for row in letters:
			if row.status != "Drafted":
				self.assertEqual(row.status, "Duplicate")
				self.assertIn(drafted[0].purchase_invoice, row.processing_note)

	def test_a_routing_rule_stops_a_letter_before_any_supplier_is_looked_for(self):
		frappe.get_doc(
			{
				"doctype": "ePost Rule",
				"title": "reminders are not booked",
				"company": self.company,
				"document_kind": "Reminder",
				"set_status": "Not Bookable",
			}
		).insert(ignore_permissions=True)

		with _returning(_invoice(document_kind="Reminder")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Not Bookable")
		self.assertFalse(doc.purchase_invoice)

	def test_a_drafted_letter_is_left_alone(self):
		with _returning(_invoice()):
			sync_letters()
			doc = self.letter_doc("inbox-1")
			self.assertEqual(doc.status, "Drafted")

			self.assertFalse(process_letter(doc))

		self.assertEqual(self.letter_doc("inbox-1").status, "Drafted")
