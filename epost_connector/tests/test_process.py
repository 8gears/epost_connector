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
from epost_connector.inbox.process import (
	mark_not_bookable,
	process_letter,
	process_letter_by_name,
	review,
)
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

			process_letter_by_name(doc.name, confirmed=True)

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Drafted")
		self.assertTrue(doc.purchase_invoice)
		alias = frappe.db.get_value("Supplier Alias", {"supplier": self.supplier}, "alias_name")
		self.assertEqual(alias, "Brand New Vendor GmbH", "the mapping is remembered")
		notes = frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "review_notes")
		self.assertIn("chosen by a reviewer", notes)

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

	def reminder_rule(self) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "ePost Rule",
					"title": "reminders are not booked",
					"company": self.company,
					"document_kind": "Reminder",
					"set_status": "Not Bookable",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_a_routing_rule_stops_a_letter_for_review_not_for_good(self):
		"""The rule acts on the type the model read, which may be wrong."""
		rule = self.reminder_rule()

		with _returning(_invoice(document_kind="Reminder")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Needs Review")
		self.assertIn(rule, doc.processing_note)
		self.assertEqual(doc.document_kind, "Reminder")
		self.assertFalse(doc.purchase_invoice)

	def test_a_drafted_letter_is_left_alone(self):
		with _returning(_invoice()):
			sync_letters()
			doc = self.letter_doc("inbox-1")
			self.assertEqual(doc.status, "Drafted")

			self.assertFalse(process_letter(doc))

		self.assertEqual(self.letter_doc("inbox-1").status, "Drafted")

	def test_an_invoice_dated_before_any_fiscal_year_waits_for_review(self):
		with _returning(_invoice(invoice_date="1999-01-15")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Needs Review")
		self.assertIn("fiscal year", doc.processing_note)
		self.assertFalse(doc.purchase_invoice)

	def test_a_supplier_already_on_the_letter_is_used_but_not_learned(self):
		"""A value left on the letter is no reviewer's decision; it must not spread."""
		other = ensure_supplier("Pipeline Other Supplier AG")
		with _returning(_invoice(vendor_name="Someone Unrelated AG")):
			sync_letters()
			doc = self.letter_doc("inbox-1")
			doc.flags.reprocess = True
			doc.status = "Downloaded"
			doc.supplier = other
			doc.purchase_invoice = None
			doc.save(ignore_permissions=True)
			process_letter(doc, force=True)

		self.assertEqual(frappe.db.count("Supplier Alias", {"alias_name": "Someone Unrelated AG"}), 0)

	def test_a_confirmed_supplier_releases_other_letters_from_the_same_issuer(self):
		from epost_connector.inbox.process import retry_waiting

		with _returning(_invoice(vendor_name="Brand New Vendor GmbH", vendor_tax_id="CHE-777.666.555 MWST")):
			sync_letters()
			waiting = frappe.get_all("ePost Letter", filters={"status": "Waiting for Supplier"}, pluck="name")
			self.assertGreater(len(waiting), 1, "the mock letterbox should hold several letters")

			first = frappe.get_doc("ePost Letter", waiting[0])
			with patch("frappe.enqueue"):
				first.supplier = self.supplier
				first.save(ignore_permissions=True)
				process_letter_by_name(first.name, confirmed=True)

			result = retry_waiting()

		self.assertEqual(result["matched"], len(waiting) - 1)
		self.assertEqual(frappe.db.count("ePost Letter", {"status": "Waiting for Supplier"}), 0)


class KindDoubtTest(ProcessTestCase):
	def test_a_non_invoice_that_carries_an_invoice_number_total_and_vat_is_doubted(self):
		"""The Cornèrcard case: skipped on the model's word, though it looked like an invoice."""
		with _returning(_invoice(document_kind="Correspondence")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Needs Review")
		self.assertIn("may be an invoice", doc.processing_note)

	def test_a_credit_note_the_letter_never_calls_one_is_doubted(self):
		with (
			_returning(_invoice(document_kind="Credit Note")),
			patch(
				"epost_connector.inbox.process._letter_text", return_value="Rechnung Nr. 5 Total CHF 108.10"
			),
		):
			sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").status, "Needs Review")

	def test_an_invoice_that_mentions_a_credit_is_doubted(self):
		with (
			_returning(_invoice()),
			patch("epost_connector.inbox.process._letter_text", return_value="Gutschrift Nr. 5 CHF 108.10"),
		):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Needs Review")
		self.assertIn("credit note", doc.processing_note)

	def test_a_plain_invoice_is_not_doubted(self):
		with (
			_returning(_invoice()),
			patch("epost_connector.inbox.process._letter_text", return_value="Rechnung Nr. 5 CHF 108.10"),
		):
			sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").status, "Drafted")


class ReviewTest(ProcessTestCase):
	"""A person corrects what the pipeline decided, at whatever status it reached."""

	def setUp(self) -> None:
		super().setUp()
		self.other = ensure_supplier("Pipeline Other Supplier AG")
		frappe.db.set_value("Supplier", self.other, "tax_id", None)
		frappe.db.delete("Supplier Alias", {"supplier": self.other})
		frappe.db.commit()

	def drafted(self, **values):
		with _returning(_invoice(**values)):
			sync_letters()
		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Drafted")
		return doc

	def review(self, doc, **values):
		with patch("frappe.enqueue"):
			review(doc.name, **values)
		return self.letter_doc(doc.letter_id)

	def test_a_wrong_supplier_on_a_draft_is_replaced_and_the_draft_rebuilt(self):
		doc = self.drafted()
		old_invoice = doc.purchase_invoice

		doc = self.review(doc, document_kind="Invoice", supplier=self.other)

		self.assertEqual(doc.status, "Drafted")
		self.assertFalse(frappe.db.exists("Purchase Invoice", old_invoice))
		invoice = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		self.assertEqual((invoice.supplier, invoice.docstatus), (self.other, 0))
		self.assertIn("chosen by a reviewer", invoice.review_notes)
		self.assertTrue(doc.reviewed)
		self.assertTrue(
			frappe.db.exists("File", {"file_url": doc.file, "attached_to_doctype": "ePost Letter"})
		)

	def test_the_correction_unlearns_the_wrong_match_and_learns_the_right_one(self):
		frappe.get_doc(
			{"doctype": "Supplier Alias", "alias_name": "Issuer Printed Name", "supplier": self.supplier}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Supplier", self.supplier, "tax_id", "CHE-123.456.789 MWST")
		doc = self.drafted(vendor_name="Issuer Printed Name", vendor_tax_id="CHE-123.456.789")

		self.review(doc, document_kind="Invoice", supplier=self.other)

		self.assertFalse(frappe.db.exists("Supplier Alias", {"supplier": self.supplier}))
		self.assertFalse(frappe.db.get_value("Supplier", self.supplier, "tax_id"))
		self.assertEqual(frappe.db.get_value("Supplier", self.other, "tax_id"), "CHE-123.456.789")
		self.assertEqual(
			frappe.db.get_value("Supplier Alias", {"alias_name": "Issuer Printed Name"}, "supplier"),
			self.other,
		)

	def test_a_credit_note_read_as_an_invoice_is_rebuilt_as_a_return(self):
		doc = self.drafted()

		doc = self.review(doc, document_kind="Credit Note", supplier=self.supplier)

		self.assertEqual(frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "is_return"), 1)

	def test_a_type_that_is_never_booked_removes_the_draft(self):
		doc = self.drafted()
		old_invoice = doc.purchase_invoice

		doc = self.review(doc, document_kind="Reminder", supplier=self.supplier)

		self.assertEqual(doc.status, "Not Bookable")
		self.assertFalse(doc.purchase_invoice)
		self.assertFalse(frappe.db.exists("Purchase Invoice", old_invoice))

	def test_an_invoice_a_rule_stopped_is_drafted_once_a_person_says_so(self):
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
		self.assertEqual(doc.status, "Needs Review")

		doc = self.review(doc, document_kind="Invoice", supplier=self.supplier)

		self.assertEqual(doc.status, "Drafted")
		self.assertEqual(doc.document_kind, "Invoice")

	def test_a_misread_number_is_not_a_duplicate_once_a_person_says_so(self):
		with _returning(_invoice(), same_number=True):
			sync_letters()
		duplicate = frappe.get_all("ePost Letter", filters={"status": "Duplicate"}, pluck="name")[0]
		doc = frappe.get_doc("ePost Letter", duplicate)

		doc = self.review(doc, document_kind="Invoice", supplier=self.supplier, not_duplicate=1)

		self.assertEqual(doc.status, "Drafted")

	def test_a_new_supplier_can_be_created_from_the_review(self):
		doc = self.drafted()
		name = "Pipeline Created In Review AG"
		frappe.db.delete("Supplier", {"supplier_name": name})

		doc = self.review(doc, document_kind="Invoice", new_supplier_name=name, tax_id="CHE-222.333.444")

		self.assertEqual(frappe.db.get_value("Supplier", doc.supplier, "supplier_name"), name)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "supplier"), doc.supplier
		)
		frappe.delete_doc("Purchase Invoice", doc.purchase_invoice, force=True, ignore_permissions=True)
		frappe.db.set_value("ePost Letter", doc.name, "purchase_invoice", None)
		frappe.delete_doc("Supplier", doc.supplier, force=True, ignore_permissions=True)

	def test_a_submitted_invoice_is_not_touched(self):
		doc = self.drafted()
		frappe.db.set_value("Purchase Invoice", doc.purchase_invoice, "docstatus", 1)
		try:
			with self.assertRaises(frappe.ValidationError):
				review(doc.name, document_kind="Invoice", supplier=self.other)
			self.assertEqual(
				frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "supplier"), self.supplier
			)
		finally:
			frappe.db.set_value("Purchase Invoice", doc.purchase_invoice, "docstatus", 0)

	def test_marking_not_bookable_records_who_decided(self):
		with _returning(_invoice(invoice_date="1999-01-15")):
			sync_letters()
		doc = self.letter_doc("inbox-1")

		mark_not_bookable([doc.name])

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Not Bookable")
		self.assertIn(frappe.session.user, doc.processing_note)
