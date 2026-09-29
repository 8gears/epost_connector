"""The Frappe Flow extractor, with the model replaced by a fixed answer.

What is tested is what the extractor does with an answer: which input it sends,
how it maps the fields, and how the computed confidence reacts to an answer
that does not add up. Whether a real model reads a real invoice correctly is
measured against booked invoices on a site, not asserted here.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from epost_connector.epost.sync import sync_letters
from epost_connector.extraction import flow
from epost_connector.extraction.flow import FlowExtractor
from epost_connector.tests.site_base import ePostSiteTestCase

TEXT = "Rechnung RE-7\n" + "Leistung Mai " * 40

ANSWER = {
	"document_kind": "Invoice",
	"vendor_name": "Muster Elektro AG",
	"vendor_tax_id": "CHE-123.456.789 MWST",
	"vendor_country": "Switzerland",
	"invoice_number": "RE-7",
	"invoice_date": "2024-05-02",
	"due_date": "2024-06-01",
	"currency": "CHF",
	"net_amount": 100.0,
	"vat_amount": 8.1,
	"gross_amount": 108.1,
	"vat_breakdown": [{"rate": 8.1, "net": 100.0, "vat": 8.1}],
	"line_items": [{"description": "Leistung Mai", "net": 100.0, "vat_rate": 8.1}],
	"summary": "Electrical work in May.",
	"confidence": 0.9,
}


class FlowExtractorTest(ePostSiteTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.configure_settings(flow_model="Test Model")
		self.letter = frappe._dict(title="Rechnung", sender_name="Muster Elektro AG")

	def extract(self, answer, text=TEXT):
		with (
			patch.object(flow, "_pdf_text", return_value=text),
			patch.object(flow, "ask_json", return_value=(answer, {"prompt_tokens": 10})) as ask,
		):
			result = FlowExtractor().extract(self.letter, b"%PDF-1.4 bytes")
		return result, ask

	def test_a_consistent_answer_keeps_the_models_confidence(self):
		result, _ = self.extract(dict(ANSWER))

		self.assertEqual(result.invoice_number, "RE-7")
		self.assertEqual(result.gross_amount, 108.1)
		self.assertEqual(result.vendor_country, "Switzerland")
		self.assertEqual(result.document_kind, "Invoice")
		self.assertEqual(result.confidence, 0.9)
		self.assertTrue(all(v is not False for v in result.raw["checks"].values()))
		self.assertEqual(result.raw["usage"], {"prompt_tokens": 10})

	def test_amounts_that_do_not_add_up_lower_the_confidence(self):
		result, _ = self.extract({**ANSWER, "gross_amount": 118.1})

		self.assertFalse(result.raw["checks"]["totals_add_up"])
		self.assertAlmostEqual(result.confidence, 0.9 - flow.PENALTIES["totals_add_up"])

	def test_due_before_invoice_date_lowers_the_confidence(self):
		result, _ = self.extract({**ANSWER, "due_date": "2024-04-01"})

		self.assertFalse(result.raw["checks"]["dates_valid"])

	def test_an_unknown_country_is_dropped(self):
		result, _ = self.extract({**ANSWER, "vendor_country": "Atlantis"})

		self.assertIsNone(result.vendor_country)

	def test_the_text_layer_is_sent_when_there_is_one(self):
		_, ask = self.extract(dict(ANSWER))

		messages = ask.call_args.args[1]
		self.assertIsInstance(messages[1]["content"], str)
		self.assertIn("Leistung Mai", messages[1]["content"])

	def test_a_scan_without_text_is_sent_as_the_pdf(self):
		_, ask = self.extract(dict(ANSWER), text="")

		content = ask.call_args.args[1][1]["content"]
		self.assertEqual(content[1]["type"], "file")
		self.assertTrue(content[1]["file"]["file_data"].startswith("data:application/pdf;base64,"))

	def test_list_fields_of_the_wrong_shape_are_ignored(self):
		result, _ = self.extract({**ANSWER, "line_items": {"a": 1}, "vat_breakdown": "8.1"})

		self.assertEqual(result.line_items, [])
		self.assertEqual(result.vat_breakdown, [])

	def test_no_answer_is_no_result(self):
		result, _ = self.extract(None)

		self.assertIsNone(result)

	def test_without_a_model_configured_nothing_is_asked(self):
		self.configure_settings(flow_model=None)
		with patch.object(flow, "ask_json") as ask:
			self.assertIsNone(FlowExtractor().extract(self.letter, b"%PDF"))
		ask.assert_not_called()


class FlowPipelineTest(ePostSiteTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()
		self.configure_settings(extractor="Flow", flow_model="Test Model")

	def test_a_correspondence_letter_is_classified_and_its_extraction_section_stays_empty(self):
		answer = {"document_kind": "Correspondence", "summary": "A newsletter.", "confidence": 0.8}
		with (
			patch.object(flow, "_pdf_text", return_value=TEXT),
			patch.object(flow, "ask_json", return_value=(answer, None)),
		):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.document_kind, "Correspondence")
		self.assertFalse(doc.vendor_name or doc.invoice_number or doc.amount)
		self.assertFalse(doc.booking_suggestion, "correspondence is not booked")
		self.assertEqual(frappe.get_last_doc("ePost Sync Log").status, "Success")

	def test_the_new_fields_land_on_the_letter(self):
		with (
			patch.object(flow, "_pdf_text", return_value=TEXT),
			patch.object(flow, "ask_json", return_value=(dict(ANSWER), None)),
		):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Analyzed")
		self.assertEqual(doc.vendor_tax_id, "CHE-123.456.789 MWST")
		self.assertEqual(doc.net_amount, 100.0)
		self.assertEqual(doc.amount, 108.1)
