"""Suggesting account and VAT template for an extracted invoice.

The order of the sources is the behaviour under test: a rule set before history
wins, history wins over the model, and a rule set after history fills only what
is still open, except that it may overrule a VAT template the model guessed.
The model is never called for real; it is replaced by a function returning a
fixed answer, so what is tested is what the code does with an answer.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe

from epost_connector.booking import backtest, history
from epost_connector.booking.suggest import (
	AFTER_HISTORY,
	BEFORE_HISTORY,
	BookingContext,
	VatGroup,
	suggest,
)
from epost_connector.tests.booking_fixtures import (
	book_invoice,
	ensure_tax_setup,
	expense_accounts,
	purge_booked,
)
from epost_connector.tests.site_base import (
	ensure_company,
	ensure_fiscal_years,
	ensure_supplier,
	ePostSiteTestCase,
)

KNOWN = "Booking Known Supplier"
SPLIT = "Booking Split Supplier"
NEW = "Booking New Supplier"
SUPPLIERS = [KNOWN, SPLIT, NEW]


class BookingTestCase(ePostSiteTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.company = ensure_company()
		ensure_fiscal_years(2021, 2024, datetime.date.today().year)
		self.templates = ensure_tax_setup(self.company)
		self.acct_a, self.acct_b, self.acct_c = expense_accounts(self.company)
		purge_booked(SUPPLIERS)
		self.addCleanup(purge_booked, SUPPLIERS)
		frappe.local.epost_charged_rates = {}

	def context(self, supplier=None, groups=None, **values) -> BookingContext:
		return BookingContext(
			company=self.company,
			supplier=supplier,
			groups=groups or [VatGroup(net=100.0, rate=8.1)],
			**values,
		)

	def rule(self, **values) -> str:
		doc = frappe.get_doc(
			{"doctype": "ePost Booking Rule", "title": "test rule", "company": self.company, **values}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name


class ChargedRateTest(BookingTestCase):
	def test_input_tax_is_the_rate_the_supplier_charged(self):
		self.assertAlmostEqual(history.charged_rate(self.templates["input"]), 8.1)

	def test_reverse_charge_nets_to_zero_because_the_supplier_charged_none(self):
		self.assertAlmostEqual(history.charged_rate(self.templates["reverse"]), 0.0)

	def test_a_template_without_a_purchase_template_has_no_known_rate(self):
		self.assertIsNone(history.charged_rate("No Such Template"))


class HistoryTest(BookingTestCase):
	def test_a_supplier_always_booked_one_way_is_booked_that_way(self):
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)])
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 50)])

		(line,) = suggest(self.context(KNOWN)).lines

		self.assertEqual(line.expense_account, self.acct_a)
		self.assertEqual(line.item_tax_template, self.templates["input"])
		self.assertEqual(line.account_source, "History")
		self.assertEqual(line.confidence, 1.0)

	def test_the_leading_share_is_used_and_reported_as_the_confidence(self):
		book_invoice(self.company, SPLIT, [(self.acct_a, self.templates["input"], 60)])
		book_invoice(self.company, SPLIT, [(self.acct_b, self.templates["input"], 40)])

		(line,) = suggest(self.context(SPLIT)).lines

		self.assertEqual(line.expense_account, self.acct_a)
		self.assertAlmostEqual(line.confidence, 0.6, places=2)

	def test_a_history_split_three_ways_suggests_nothing(self):
		book_invoice(self.company, SPLIT, [(self.acct_a, self.templates["input"], 40)])
		book_invoice(self.company, SPLIT, [(self.acct_b, self.templates["input"], 35)])
		book_invoice(self.company, SPLIT, [(self.acct_c, self.templates["input"], 25)])

		(line,) = suggest(self.context(SPLIT)).lines

		self.assertIsNone(line.expense_account)
		self.assertEqual(line.account_source, "None")

	def test_each_vat_rate_is_booked_from_the_history_with_that_rate(self):
		book_invoice(
			self.company,
			KNOWN,
			[(self.acct_a, self.templates["input"], 100), (self.acct_b, self.templates["reverse"], 300)],
		)

		lines = suggest(
			self.context(KNOWN, groups=[VatGroup(net=10, rate=8.1), VatGroup(net=20, rate=0.0)])
		).lines

		self.assertEqual(
			[(line.expense_account, line.item_tax_template) for line in lines],
			[(self.acct_a, self.templates["input"]), (self.acct_b, self.templates["reverse"])],
		)

	def test_without_a_cut_off_date_older_bookings_weigh_less(self):
		"""Production passes no date; age must still count, relative to today."""
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)], "2021-06-01")
		book_invoice(
			self.company,
			KNOWN,
			[(self.acct_b, self.templates["input"], 60)],
			datetime.date.today().isoformat(),
		)

		(line,) = suggest(self.context(KNOWN)).lines

		self.assertEqual(line.expense_account, self.acct_b)

	def test_one_account_and_template_across_cost_centers_is_one_booking(self):
		from epost_connector.tests.site_base import cost_center

		first = cost_center(self.company)
		second = _second_cost_center(self.company)
		self.assertNotEqual(first, second)
		book_invoice(
			self.company, SPLIT, [(self.acct_a, self.templates["input"], 40)], cost_center_name=first
		)
		book_invoice(
			self.company, SPLIT, [(self.acct_a, self.templates["input"], 35)], cost_center_name=second
		)
		book_invoice(
			self.company, SPLIT, [(self.acct_b, self.templates["input"], 25)], cost_center_name=first
		)

		(line,) = suggest(self.context(SPLIT)).lines

		self.assertEqual(line.expense_account, self.acct_a)
		self.assertAlmostEqual(line.confidence, 0.75, places=2)
		self.assertEqual(line.cost_center, first)

	def test_history_after_the_cut_off_date_is_not_used(self):
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)], "2024-06-01")

		(line,) = suggest(self.context(KNOWN), as_of="2024-05-01").lines

		self.assertIsNone(line.expense_account)


class RuleTest(BookingTestCase):
	def test_a_rule_before_history_wins_over_the_history(self):
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)])
		rule = self.rule(apply_when=BEFORE_HISTORY, supplier=KNOWN, expense_account=self.acct_b)

		(line,) = suggest(self.context(KNOWN)).lines

		self.assertEqual(line.expense_account, self.acct_b)
		self.assertEqual(line.account_source, "Rule")
		self.assertIn(rule, line.evidence)
		self.assertEqual(line.item_tax_template, self.templates["input"], "history still fills the template")

	def test_a_rule_after_history_only_fills_what_history_left_open(self):
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)])
		self.rule(apply_when=AFTER_HISTORY, expense_account=self.acct_b)

		(line,) = suggest(self.context(KNOWN)).lines

		self.assertEqual(line.expense_account, self.acct_a)

	def test_a_template_rule_is_skipped_when_its_rate_is_not_the_charged_rate(self):
		self.rule(
			apply_when=BEFORE_HISTORY, expense_account=self.acct_a, item_tax_template=self.templates["input"]
		)

		(line,) = suggest(self.context(NEW, groups=[VatGroup(net=50, rate=0.0)])).lines

		self.assertEqual(line.expense_account, self.acct_a)
		self.assertIsNone(line.item_tax_template)

	def test_foreign_and_no_vat_charged_selects_reverse_charge(self):
		self.rule(
			apply_when=AFTER_HISTORY,
			vat_on_invoice="Not charged",
			foreign_only=1,
			item_tax_template=self.templates["reverse"],
		)
		supplier = ensure_supplier(NEW)
		self.rule(apply_when=BEFORE_HISTORY, supplier=supplier, expense_account=self.acct_c)

		(line,) = suggest(
			self.context(supplier, groups=[VatGroup(net=50, rate=0.0)], vendor_country="Germany")
		).lines

		self.assertEqual(line.item_tax_template, self.templates["reverse"])

	def test_a_rule_needs_something_to_set(self):
		with self.assertRaises(frappe.ValidationError):
			self.rule(apply_when=BEFORE_HISTORY, supplier=KNOWN)

	def test_a_rule_needs_a_company_because_its_targets_have_one(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self.rule(apply_when=BEFORE_HISTORY, company=None, expense_account=self.acct_a)
		self.assertIn("Company", str(caught.exception))


class LlmFallbackTest(BookingTestCase):
	def test_the_model_books_what_rules_and_history_left_open(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["input"], 100)])
		answer = {
			"lines": [
				{
					"index": 0,
					"expense_account": self.acct_b,
					"item_tax_template": self.templates["input"],
					"confidence": 0.9,
					"reason": "like Known",
				}
			]
		}

		with patch("epost_connector.booking.llm.ask_json", return_value=(answer, {"prompt_tokens": 1})):
			(line,) = suggest(self.context(NEW), llm_model="any").lines

		self.assertEqual(line.expense_account, self.acct_b)
		self.assertEqual(line.account_source, "LLM")
		self.assertEqual(line.confidence, 0.6, "a model never outranks history")

	def test_an_account_the_company_never_booked_to_is_rejected(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["input"], 100)])
		answer = {
			"lines": [
				{
					"index": 0,
					"expense_account": self.acct_c,
					"item_tax_template": self.templates["input"],
					"confidence": 1,
					"reason": "x",
				}
			]
		}

		with patch("epost_connector.booking.llm.ask_json", return_value=(answer, None)):
			(line,) = suggest(self.context(NEW), llm_model="any").lines

		self.assertIsNone(line.expense_account)
		self.assertEqual(line.account_source, "None")

	def test_a_rule_after_history_overrules_the_models_vat_template(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["reverse"], 100)])
		book_invoice(self.company, SPLIT, [(self.acct_b, self.templates["input"], 100)])
		self.rule(
			apply_when=AFTER_HISTORY,
			vat_on_invoice="Not charged",
			item_tax_template=self.templates["reverse"],
		)
		answer = {
			"lines": [
				{
					"index": 0,
					"expense_account": self.acct_b,
					"item_tax_template": self.templates["input"],
					"confidence": 1,
					"reason": "x",
				}
			]
		}

		with patch("epost_connector.booking.llm.ask_json", return_value=(answer, None)):
			(line,) = suggest(self.context(NEW, groups=[VatGroup(net=10, rate=0.0)]), llm_model="any").lines

		self.assertEqual(line.item_tax_template, self.templates["reverse"])
		self.assertEqual(line.template_source, "Default")

	def test_a_malformed_answer_leaves_the_line_open_instead_of_failing(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["input"], 100)])
		for answer in (
			{"lines": {}},
			{"lines": ["x", 3]},
			{"lines": [{"index": 0, "expense_account": [1], "item_tax_template": None}]},
			{"lines": [{"index": True, "expense_account": self.acct_b, "item_tax_template": None}]},
		):
			with (
				self.subTest(answer=answer),
				patch("epost_connector.booking.llm.ask_json", return_value=(answer, None)) as ask,
			):
				(line,) = suggest(self.context(NEW), llm_model="any").lines
				ask.assert_called_once()
				self.assertIsNone(line.expense_account)

	def test_an_integral_float_index_is_accepted(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["input"], 100)])
		answer = {
			"lines": [
				{"index": 0.0, "expense_account": self.acct_b, "item_tax_template": None, "confidence": 0.5}
			]
		}

		with patch("epost_connector.booking.llm.ask_json", return_value=(answer, None)):
			(line,) = suggest(self.context(NEW), llm_model="any").lines

		self.assertEqual(line.expense_account, self.acct_b)

	def test_a_model_template_for_another_vat_rate_is_dropped_but_the_account_kept(self):
		book_invoice(self.company, KNOWN, [(self.acct_b, self.templates["input"], 100)])
		answer = {
			"lines": [
				{
					"index": 0,
					"expense_account": self.acct_b,
					"item_tax_template": self.templates["input"],
					"confidence": 0.5,
					"reason": "x",
				}
			]
		}

		with patch("epost_connector.booking.llm.ask_json", return_value=(answer, None)):
			(line,) = suggest(self.context(NEW, groups=[VatGroup(net=10, rate=0.0)]), llm_model="any").lines

		self.assertEqual(line.expense_account, self.acct_b)
		self.assertIsNone(line.item_tax_template)

	def test_without_a_model_nothing_is_asked(self):
		with patch("epost_connector.booking.llm.ask_json") as ask:
			suggest(self.context(NEW))
		ask.assert_not_called()


class BacktestTest(BookingTestCase):
	def test_a_supplier_booked_the_same_way_twice_scores_on_the_second_invoice(self):
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 100)], "2024-03-01")
		book_invoice(self.company, KNOWN, [(self.acct_a, self.templates["input"], 80)], "2024-04-01")

		result = backtest.run(self.company)

		history_tally = result["by_source"]["History"]
		self.assertEqual(history_tally["lines"], 1)
		self.assertEqual(history_tally["account_accuracy"], 1.0)
		self.assertEqual(result["by_source"]["None"]["lines"], 1, "the first invoice had no history")


class LetterSuggestionTest(BookingTestCase):
	def test_a_letter_that_stops_being_bookable_loses_its_old_suggestion(self):
		from epost_connector.booking.letter import suggest_for_letter
		from epost_connector.epost.sync import sync_letters

		self.state.content_override.clear()
		sync_letters()
		letter = self.letter_doc("inbox-1")
		letter.update(
			{
				"amount": 10,
				"document_kind": "Contract",
				"booking_suggestion": '{"lines": [{"net": 10}]}',
				"booking_source": "History",
			}
		)

		self.assertIsNone(suggest_for_letter(letter))
		self.assertIsNone(letter.booking_suggestion)
		self.assertIsNone(letter.booking_source)


class AnalyzeAllTest(ePostSiteTestCase):
	def test_it_refuses_to_queue_work_the_no_op_extractor_cannot_do(self):
		from epost_connector.extraction.pipeline import analyze_all

		with self.assertRaises(frappe.ValidationError):
			analyze_all()


def _second_cost_center(company: str) -> str:
	from epost_connector.tests.site_base import TEST_COMPANY_ABBR

	name = f"Booking Test Center - {TEST_COMPANY_ABBR}"
	if not frappe.db.exists("Cost Center", name):
		parent = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
		frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": "Booking Test Center",
				"parent_cost_center": parent,
				"company": company,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	return name
