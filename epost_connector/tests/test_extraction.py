"""The extract stage.

The app ships no working extractor on purpose, so most of what is worth testing
is the seam: that the registry resolves to the no-op, that the no-op costs
nothing, and that a real extractor plugged in behind it moves a letter along and
lands its answer on the right fields. The last one is what the seam exists for,
so it is tested with an extractor registered inside the test rather than assumed.
"""

from __future__ import annotations

import contextlib
import datetime

import frappe

from epost_connector.epost.sync import sync_letters
from epost_connector.extraction import registry
from epost_connector.extraction.base import ExtractionResult, LetterExtractor
from epost_connector.extraction.noop import NoopExtractor
from epost_connector.extraction.pipeline import analyze, analyze_letter
from epost_connector.extraction.registry import get_extractor
from epost_connector.tests.site_base import ePostSiteTestCase, registered_extractor


class RegistryTest(ePostSiteTestCase):
	def test_the_unset_field_and_the_named_none_both_resolve_to_the_noop(self):
		for key in ("", None, "None"):
			with self.subTest(key=key):
				self.assertIsInstance(get_extractor(key), NoopExtractor)

	def test_whitespace_around_the_key_is_tolerated(self):
		self.assertIsInstance(get_extractor("  None  "), NoopExtractor)

	def test_an_unregistered_name_is_refused_by_name(self):
		with self.assertRaises(ValueError) as caught:
			get_extractor("Anthropic")

		self.assertIn("Anthropic", str(caught.exception))
		self.assertIn("Registered extractors", str(caught.exception))

	def test_the_settings_field_refuses_a_name_it_has_no_option_for(self):
		settings = frappe.get_doc("ePost Settings")
		settings.extractor = "Anthropic"

		with self.assertRaises(frappe.exceptions.ValidationError):
			settings.save(ignore_permissions=True)

	def test_the_select_options_and_the_registry_agree(self):
		"""Two lists that have to be edited together, and are easy to edit apart.

		A key in the registry but not the options cannot be chosen; an option
		with no key behind it is a setting that fails at extraction time, on a
		letter, in a background job.
		"""
		options = frappe.get_meta("ePost Settings").get_field("extractor").options.split("\n")

		self.assertEqual(set(options), set(registry.EXTRACTORS))

	def test_every_registered_key_instantiates(self):
		for key in registry.EXTRACTORS:
			with self.subTest(key=key):
				self.assertIsInstance(get_extractor(key), LetterExtractor)

	def test_the_noop_reads_nothing_out_of_anything(self):
		self.assertIsNone(NoopExtractor().extract(None, b"%PDF-1.4 whatever"))


class NoopPipelineTest(ePostSiteTestCase):
	def test_a_letter_stops_at_downloaded_while_the_extractor_is_none(self):
		self.state.content_override.clear()
		sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Downloaded")
		self.assertIsNone(doc.vendor_name)
		self.assertIsNone(doc.invoice_number)

	def test_the_noop_never_reads_the_pdf_off_the_disk(self):
		"""It runs on every letter of every hourly sync; it must cost nothing."""
		self.state.content_override.clear()
		sync_letters()
		doc = self.letter_doc("inbox-1")

		with _no_file_reads() as opened:
			self.assertFalse(analyze_letter(doc, force=True))

		self.assertEqual(opened, [])

	def test_a_letter_with_no_pdf_is_not_analysed(self):
		sync_letters()
		doc = self.letter_doc("inbox-html-error")

		self.assertFalse(doc.file)
		self.assertFalse(analyze_letter(doc, force=True))


class ExtractorPipelineTest(ePostSiteTestCase):
	"""What happens when a real extractor is plugged into the seam."""

	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()

	def test_a_result_advances_the_status_and_lands_on_the_fields(self):
		result = ExtractionResult(
			vendor_name="Muster Elektro AG",
			invoice_number="RE-2024-0042",
			invoice_date="2024-03-01",
			due_date="2024-03-31",
			currency="CHF",
			net_amount=100.0,
			vat_amount=8.1,
			gross_amount=108.1,
			confidence=0.87,
			raw={"engine": "test"},
		)

		with _extractor_returning(result):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Analyzed")
		self.assertEqual(doc.vendor_name, "Muster Elektro AG")
		self.assertEqual(doc.invoice_number, "RE-2024-0042")
		self.assertEqual(doc.invoice_date, datetime.date(2024, 3, 1))
		self.assertEqual(doc.due_date, datetime.date(2024, 3, 31))
		self.assertEqual(doc.currency, "CHF")
		self.assertEqual(doc.amount, 108.1)
		self.assertEqual(doc.vat_amount, 8.1)
		self.assertEqual(doc.extraction_confidence, 0.87)
		self.assertEqual(frappe.parse_json(doc.extraction_raw), {"engine": "test"})

	def test_the_gross_amount_wins_over_the_net_one(self):
		with _extractor_returning(ExtractionResult(net_amount=100.0, gross_amount=108.1)):
			sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").amount, 108.1)

	def test_the_net_amount_is_used_when_there_is_no_gross_one(self):
		with _extractor_returning(ExtractionResult(net_amount=100.0)):
			sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").amount, 100.0)

	def test_a_currency_erpnext_does_not_know_is_dropped_not_saved(self):
		"""`currency` is a Link; an unknown code would fail validation on save."""
		with _extractor_returning(ExtractionResult(currency="XYZ", gross_amount=10.0)):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertIsNone(doc.currency)
		self.assertEqual(doc.status, "Analyzed")

	def test_a_currency_the_extractor_did_not_find_is_cleared_not_left_behind(self):
		with _extractor_returning(ExtractionResult(currency="CHF", gross_amount=10.0)):
			sync_letters()
		self.assertEqual(self.letter_doc("inbox-1").currency, "CHF")

		doc = self.letter_doc("inbox-1")
		with _extractor_returning(ExtractionResult(gross_amount=10.0)):
			analyze_letter(doc, force=True)

		self.assertIsNone(self.letter_doc("inbox-1").currency)


class DefaultedFieldTest(ePostSiteTestCase):
	"""An extraction field must mean "this was read off the document"."""

	def test_a_letter_nobody_has_read_carries_no_currency(self):
		"""Frappe fills missing Link fields from the site defaults on insert.

		A letter would otherwise arrive holding the site currency, and the
		importer reports whatever is here as detected on the letter — a claim
		about a PDF nothing has opened.
		"""
		self.state.content_override.clear()
		sync_letters()

		site_default = frappe.defaults.get_defaults().get("currency")
		self.assertTrue(site_default, "the site has no default currency; this test proves nothing")

		for letter_id in self.letter_ids():
			with self.subTest(letter=letter_id):
				self.assertIsNone(self.letter_doc(letter_id).currency)

	def test_the_other_extraction_fields_start_empty_too(self):
		self.state.content_override.clear()
		sync_letters()
		doc = self.letter_doc("inbox-1")

		for field in ("vendor_name", "invoice_number", "invoice_date", "due_date"):
			with self.subTest(field=field):
				self.assertIsNone(doc.get(field))

	def test_an_unparseable_date_is_dropped_rather_than_losing_the_result(self):
		with _extractor_returning(ExtractionResult(invoice_number="RE-1", invoice_date="not a date")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertIsNone(doc.invoice_date)
		self.assertEqual(doc.invoice_number, "RE-1")

	def test_an_extractor_returning_nothing_leaves_the_letter_at_downloaded(self):
		with _extractor_returning(None):
			summary = sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").status, "Downloaded")
		self.assertEqual(summary["letters_analyzed"], 0)

	def test_the_extractor_is_handed_the_bytes_that_were_downloaded(self):
		seen: dict[str, bytes] = {}

		with _extractor_recording(seen):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertTrue(seen[doc.letter_id].startswith(b"%PDF-"))
		# The id is in the PDF, so this proves the right letter's bytes arrived.
		self.assertIn(b"inbox-1", seen[doc.letter_id])

	def test_re_running_extraction_by_hand_forces_it_past_the_status_gate(self):
		with _extractor_returning(ExtractionResult(invoice_number="RE-1")):
			sync_letters()
			doc = self.letter_doc("inbox-1")
			self.assertEqual(doc.status, "Analyzed")

			# Already Analyzed, so only `force` gets it to run again.
			self.assertFalse(analyze_letter(doc))
			result = analyze(doc.name)

		self.assertTrue(result["extracted"])
		self.assertEqual(result["status"], "Analyzed")

	def test_a_second_sync_does_not_re_analyse_an_analysed_letter(self):
		calls: list[str] = []

		with _extractor_counting(calls):
			sync_letters()
			first = len(calls)
			sync_letters()

		self.assertEqual(len(calls), first, "extraction must not re-run on every sync")


# ----------------------------------------------------------------------
# Extractors registered for the duration of one test
# ----------------------------------------------------------------------

TEST_KEY = "TestExtractor"


def _registered(extractor_class):
	return registered_extractor(TEST_KEY, extractor_class)


def _extractor_returning(result: ExtractionResult | None):
	class Fixed(LetterExtractor):
		name = TEST_KEY

		def extract(self, letter_doc, pdf_bytes):
			return result

	return _registered(Fixed)


def _extractor_recording(into: dict[str, bytes]):
	class Recording(LetterExtractor):
		name = TEST_KEY

		def extract(self, letter_doc, pdf_bytes):
			into[letter_doc.letter_id] = pdf_bytes
			return ExtractionResult(invoice_number=letter_doc.letter_id)

	return _registered(Recording)


def _extractor_counting(into: list[str]):
	class Counting(LetterExtractor):
		name = TEST_KEY

		def extract(self, letter_doc, pdf_bytes):
			into.append(letter_doc.letter_id)
			return ExtractionResult(invoice_number="RE-1")

	return _registered(Counting)


@contextlib.contextmanager
def _no_file_reads():
	"""Record every path opened for reading while the block runs."""
	import builtins

	opened: list[str] = []
	real_open = builtins.open

	def watching_open(file, mode="r", *args, **kwargs):
		if "r" in mode and isinstance(file, str) and "/files/" in file:
			opened.append(file)
		return real_open(file, mode, *args, **kwargs)

	builtins.open = watching_open
	try:
		yield opened
	finally:
		builtins.open = real_open
