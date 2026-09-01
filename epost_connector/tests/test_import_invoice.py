"""Creating a Purchase Invoice from a letter.

The invoice is a starting point for a human, never an auto-booking. Two things
matter more than everything else here and both are asserted directly: it is left
in **draft**, and it is never created without a Supplier somebody chose or the
matcher was sure about. A wrong draft is a correction; a wrong submitted invoice
is a ledger entry, and Bexio is still the book of record during the overlap.
"""

from __future__ import annotations

import datetime

import frappe

from epost_connector.epost.import_invoice import (
	create_purchase_invoice,
	find_supplier,
	suggest_supplier,
)
from epost_connector.epost.sync import sync_letters
from epost_connector.tests.site_base import (
	TEST_CURRENCY,
	cost_center,
	ensure_company,
	ensure_fiscal_years,
	ensure_service_item,
	ensure_supplier,
	ePostSiteTestCase,
	expense_account,
)

SUPPLIER = "Muster Elektro AG"

#: The year the dated fixtures below post in, alongside today's.
FIXTURE_YEAR = 2024


class ImportTestCase(ePostSiteTestCase):
	"""A synced letterbox, a company, and a supplier the letters can match."""

	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()

		self.company = ensure_company()
		ensure_fiscal_years(FIXTURE_YEAR, datetime.date.today().year)
		self.supplier = ensure_supplier(SUPPLIER)
		self.configure_settings(
			company=self.company,
			default_expense_account=expense_account(self.company),
			default_cost_center=cost_center(self.company),
		)
		sync_letters()

	def letter_with(self, **values):
		"""A synced letter carrying the extraction fields a human would see."""
		doc = self.letter_doc("inbox-1")
		doc.update(values)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc


class DraftInvoiceTest(ImportTestCase):
	def test_the_invoice_is_created_as_a_draft_and_is_not_submitted(self):
		self.letter_with(vendor_name=SUPPLIER, amount=108.10)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.docstatus, 0, "a draft, never a posted invoice")
		self.assertEqual(invoice.supplier, self.supplier)
		self.assertEqual(invoice.company, self.company)

	def test_nothing_reaches_the_general_ledger(self):
		"""A draft posts no entries. This is the difference that matters."""
		self.letter_with(vendor_name=SUPPLIER, amount=108.10)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		entries = frappe.get_all(
			"GL Entry", filters={"voucher_type": "Purchase Invoice", "voucher_no": result["purchase_invoice"]}
		)
		self.assertEqual(entries, [])

	def test_the_letter_is_linked_back_and_marked_imported(self):
		self.letter_with(vendor_name=SUPPLIER, amount=108.10)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.purchase_invoice, result["purchase_invoice"])
		self.assertEqual(doc.status, "Imported")
		self.assertEqual(doc.supplier, self.supplier)

	def test_the_letter_pdf_is_attached_to_the_invoice(self):
		letter = self.letter_with(vendor_name=SUPPLIER, amount=108.10)

		result = create_purchase_invoice(letter.name)

		attached = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Purchase Invoice",
				"attached_to_name": result["purchase_invoice"],
			},
			fields=["file_url", "is_private"],
		)
		self.assertEqual(len(attached), 1)
		# The same stored file, not a second copy of the bytes.
		self.assertEqual(attached[0].file_url, letter.file)
		self.assertEqual(attached[0].is_private, 1)

	def test_the_invoice_carries_the_extracted_numbers_and_dates(self):
		self.letter_with(
			vendor_name=SUPPLIER,
			invoice_number="RE-2024-0042",
			invoice_date="2024-03-01",
			due_date="2024-03-31",
			amount=108.10,
		)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.bill_no, "RE-2024-0042")
		self.assertEqual(invoice.bill_date, datetime.date(2024, 3, 1))
		self.assertEqual(invoice.posting_date, datetime.date(2024, 3, 1))
		self.assertEqual(invoice.due_date, datetime.date(2024, 3, 31))
		self.assertEqual(len(invoice.items), 1)
		self.assertEqual(invoice.items[0].rate, 108.10)
		self.assertEqual(invoice.items[0].qty, 1)

	def test_the_invoice_names_the_letter_it_came_from(self):
		self.letter_with(vendor_name=SUPPLIER, amount=10.0)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		remarks = frappe.db.get_value("Purchase Invoice", result["purchase_invoice"], "remarks")
		self.assertIn("inbox-1", remarks)

	def test_a_due_date_before_the_posting_date_is_dropped(self):
		"""ERPNext refuses it, and a letter can carry one after a late scan."""
		self.letter_with(vendor_name=SUPPLIER, invoice_date="2024-03-31", due_date="2024-03-01", amount=10.0)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertGreaterEqual(invoice.due_date, invoice.posting_date)

	def test_a_letter_with_no_amount_still_produces_a_draft_to_complete(self):
		self.letter_with(vendor_name=SUPPLIER)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.docstatus, 0)
		self.assertEqual(invoice.items[0].rate, 0)


class InvoiceLineTest(ImportTestCase):
	"""The line the importer builds, with and without a configured Item.

	The no-Item path was flagged as the least certain thing in the app: it
	appends a line carrying only `item_name` and an expense account, and whether
	ERPNext accepts that is a question about ERPNext, not about this code.
	"""

	def test_without_a_default_item_the_line_carries_the_letter_title(self):
		self.assertFalse(frappe.db.get_single_value("ePost Settings", "default_item_code"))
		self.letter_with(vendor_name=SUPPLIER, amount=42.0)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		line = frappe.get_doc("Purchase Invoice", result["purchase_invoice"]).items[0]
		self.assertFalse(line.item_code)
		self.assertEqual(line.item_name, self.letter_doc("inbox-1").title)
		self.assertTrue(line.expense_account)
		self.assertEqual(line.rate, 42.0)

	def test_with_a_default_item_the_line_uses_it(self):
		item = ensure_service_item()
		self.configure_settings(
			company=self.company,
			default_item_code=item,
			default_expense_account=expense_account(self.company),
			default_cost_center=cost_center(self.company),
		)
		self.letter_with(vendor_name=SUPPLIER, amount=42.0)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.docstatus, 0)
		self.assertEqual(invoice.items[0].item_code, item)
		self.assertEqual(invoice.items[0].rate, 42.0)

	def test_the_line_is_a_single_unit_at_the_full_amount(self):
		"""Quantity is not a thing a letter carries; the amount is the rate."""
		self.letter_with(vendor_name=SUPPLIER, amount=1234.56)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		line = frappe.get_doc("Purchase Invoice", result["purchase_invoice"]).items[0]
		self.assertEqual(line.qty, 1)
		self.assertEqual(line.rate, 1234.56)
		self.assertEqual(line.amount, 1234.56)


class CurrencyTest(ImportTestCase):
	def test_the_company_currency_is_taken_as_it_stands(self):
		self.letter_with(vendor_name=SUPPLIER, currency=TEST_CURRENCY, amount=108.10)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.currency, TEST_CURRENCY)
		self.assertEqual(invoice.conversion_rate, 1)

	def test_a_foreign_currency_is_recorded_rather_than_guessed_at(self):
		"""A rate nobody confirmed would be a made-up number in the books."""
		self.letter_with(vendor_name=SUPPLIER, currency="EUR", amount=108.10)

		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.currency, TEST_CURRENCY)
		self.assertEqual(invoice.conversion_rate, 1)
		self.assertIn("EUR", invoice.remarks)

	def test_the_site_default_currency_never_reaches_the_invoice(self):
		"""`new_doc` fills `currency` from the site defaults, not the company's.

		Left in place it denominates the draft in a currency nobody chose, and
		ERPNext refuses it outright against a company-currency payable account —
		so on a site whose default differs from the company, no letter could be
		imported at all.
		"""
		site_default = frappe.defaults.get_defaults().get("currency")
		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		if site_default == company_currency:
			self.skipTest(f"the site default is already {company_currency}; this proves nothing")

		self.letter_with(vendor_name=SUPPLIER, amount=10.0)
		result = create_purchase_invoice(self.letter_doc("inbox-1").name)

		invoice = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(invoice.currency, company_currency)
		self.assertNotIn(site_default, invoice.remarks)


class RefusalTest(ImportTestCase):
	def test_a_letter_with_no_matchable_supplier_is_refused(self):
		self.letter_with(vendor_name="Nobody Nowhere Unmatchable", sender_name=None)

		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			create_purchase_invoice(self.letter_doc("inbox-1").name)

		self.assertIn("No Supplier matches", str(caught.exception))
		self.assertEqual(frappe.db.count("Purchase Invoice"), 0)
		self.assertNotEqual(self.letter_doc("inbox-1").status, "Imported")

	def test_a_supplier_passed_in_by_hand_overrides_the_matcher(self):
		other = ensure_supplier("Some Other Supplier")
		self.letter_with(vendor_name="Nobody Nowhere Unmatchable")

		result = create_purchase_invoice(self.letter_doc("inbox-1").name, supplier=other)

		self.assertEqual(result["supplier"], other)

	def test_a_letter_already_linked_to_an_invoice_is_refused(self):
		self.letter_with(vendor_name=SUPPLIER, amount=10.0)
		create_purchase_invoice(self.letter_doc("inbox-1").name)

		doc = self.letter_doc("inbox-1")
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			create_purchase_invoice(doc.name)

		self.assertIn("already linked", str(caught.exception))
		self.assertEqual(frappe.db.count("Purchase Invoice"), 1)

	def test_no_company_configured_and_none_by_default_is_refused(self):
		self.configure_settings(company=None)
		self.letter_with(vendor_name=SUPPLIER, amount=10.0)
		default = frappe.defaults.get_defaults().get("company")
		frappe.defaults.clear_default("company")
		self.addCleanup(lambda: default and frappe.defaults.set_default("company", default))

		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			create_purchase_invoice(self.letter_doc("inbox-1").name)

		self.assertIn("Company", str(caught.exception))


class SupplierMatchingTest(ImportTestCase):
	def test_an_exact_name_matches(self):
		letter = self.letter_with(vendor_name=SUPPLIER)
		self.assertEqual(find_supplier(letter), self.supplier)

	def test_a_legal_form_suffix_does_not_prevent_a_match(self):
		letter = self.letter_with(vendor_name="Muster Elektro")
		self.assertEqual(find_supplier(letter), self.supplier)

	def test_case_and_punctuation_do_not_prevent_a_match(self):
		letter = self.letter_with(vendor_name="muster-elektro, ag.")
		self.assertEqual(find_supplier(letter), self.supplier)

	def test_the_sender_is_used_when_no_vendor_was_extracted(self):
		letter = self.letter_with(vendor_name=None, sender_name=SUPPLIER)
		self.assertEqual(find_supplier(letter), self.supplier)

	def test_an_ambiguous_name_returns_nothing_rather_than_a_guess(self):
		"""Two plausible suppliers is a question for a human, not a coin toss.

		A name family of its own, so the exact and normalised passes both come up
		empty and the substring pass is the one that has to decide. "Nordwand
		Bau" is contained in both suppliers below and equal to neither.
		"""
		ensure_supplier("Nordwand Bau Ost AG")
		ensure_supplier("Nordwand Bau West AG")
		letter = self.letter_with(vendor_name="Nordwand Bau", sender_name=None)

		self.assertIsNone(find_supplier(letter))

	def test_one_substring_match_is_still_taken(self):
		"""The guard is ambiguity, not the substring pass itself."""
		ensure_supplier("Sudwand Bau Ost AG")
		letter = self.letter_with(vendor_name="Sudwand Bau", sender_name=None)

		self.assertEqual(find_supplier(letter), "Sudwand Bau Ost AG")

	def test_a_letter_with_nothing_to_match_on_returns_nothing(self):
		letter = self.letter_with(vendor_name=None, sender_name=None)
		self.assertIsNone(find_supplier(letter))

	def test_a_very_short_name_is_not_substring_matched(self):
		"""`in` on a two-letter string would match half the supplier list."""
		ensure_supplier("Alpha Beta Gamma AG")
		letter = self.letter_with(vendor_name="ab", sender_name=None)

		self.assertIsNone(find_supplier(letter))

	def test_the_dialog_is_offered_the_same_answer_the_import_would_take(self):
		self.letter_with(vendor_name=SUPPLIER)
		name = self.letter_doc("inbox-1").name

		self.assertEqual(suggest_supplier(name)["supplier"], self.supplier)

	def test_a_supplier_set_on_the_letter_wins_over_the_matcher(self):
		other = ensure_supplier("Chosen By Hand AG")
		letter = self.letter_with(vendor_name=SUPPLIER, supplier=other)

		self.assertEqual(suggest_supplier(letter.name)["supplier"], other)


class PermissionTest(ImportTestCase):
	"""The invoice is inserted with permissions ignored, so they are checked first."""

	ROLE = "ePost Test Letter Writer"

	def setUp(self) -> None:
		super().setUp()
		self.user = _user_with_letter_write_only(self.ROLE)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_write_on_a_letter_is_not_by_itself_a_licence_to_book(self):
		self.letter_with(vendor_name=SUPPLIER, amount=10.0)
		name = self.letter_doc("inbox-1").name

		frappe.set_user(self.user)
		with self.assertRaises(frappe.exceptions.PermissionError):
			create_purchase_invoice(name)

		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.count("Purchase Invoice"), 0)

	def test_a_user_who_may_not_touch_the_letter_is_refused(self):
		self.letter_with(vendor_name=SUPPLIER, amount=10.0)
		name = self.letter_doc("inbox-1").name

		frappe.set_user("Guest")
		with self.assertRaises(frappe.exceptions.PermissionError):
			create_purchase_invoice(name)


def _user_with_letter_write_only(role: str) -> str:
	"""A user who may edit ePost Letters and nothing else."""
	from frappe.permissions import add_permission, update_permission_property

	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		add_permission("ePost Letter", role, 0)
		update_permission_property("ePost Letter", role, 0, "write", 1)

	email = "epost-letter-writer@example.com"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "ePost",
				"last_name": "Writer",
				"send_welcome_email": 0,
			}
		)
		user.insert(ignore_permissions=True)
		user.add_roles(role)

	frappe.db.commit()
	return email
