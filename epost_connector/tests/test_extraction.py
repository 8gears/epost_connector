"""The extract stage.

What is worth testing is the seam: that the registry resolves to the no-op, that
the no-op costs nothing, and that a real extractor plugged in behind it moves a
letter along and lands its answer in an Extraction Log, never on the letter. It is
tested with an extractor registered inside the test rather than assumed.
"""

from __future__ import annotations

import contextlib
import datetime

import frappe

from epost_connector.epost.sync import sync_letters
from epost_connector.extraction import registry
from epost_connector.extraction.base import ExtractionResult, LetterExtractor
from epost_connector.extraction.noop import NoopExtractor
from epost_connector.extraction.pipeline import extract_letter
from epost_connector.extraction.registry import get_extractor
from epost_connector.inbox.process import process
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


LOG = "ePost Extraction Log"

#: Fields extraction used to write onto the letter. The letter is now the raw
#: import only; none of these may come back.
REMOVED_FIELDS = (
	"vendor_name",
	"invoice_number",
	"invoice_date",
	"due_date",
	"currency",
	"amount",
	"vat_amount",
	"extraction_raw",
	"booking_suggestion",
	"document_kind",
)


class NoopPipelineTest(ePostSiteTestCase):
	def test_a_letter_stops_at_downloaded_while_the_extractor_is_none(self):
		self.state.content_override.clear()
		sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Downloaded")
		self.assertIsNone(doc.extraction_log)

	def test_the_noop_never_reads_the_pdf_off_the_disk(self):
		"""It runs on every letter of every hourly sync; it must cost nothing."""
		self.state.content_override.clear()
		sync_letters()
		doc = self.letter_doc("inbox-1")

		with _no_file_reads() as opened:
			self.assertIsNone(extract_letter(doc, force=True))

		self.assertEqual(opened, [])

	def test_a_letter_with_no_pdf_is_not_analysed(self):
		sync_letters()
		doc = self.letter_doc("inbox-html-error")

		self.assertFalse(doc.file)
		self.assertIsNone(extract_letter(doc, force=True))


class RawLetterTest(ePostSiteTestCase):
	def test_the_letter_carries_no_extracted_fields(self):
		meta = frappe.get_meta("ePost Letter")
		for field in REMOVED_FIELDS:
			with self.subTest(field=field):
				self.assertFalse(meta.has_field(field))


class ExtractorPipelineTest(ePostSiteTestCase):
	"""What happens when a real extractor is plugged into the seam."""

	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()

	def log_of(self, letter_id: str):
		doc = self.letter_doc(letter_id)
		self.assertTrue(doc.extraction_log, "no extraction log was linked")
		return frappe.get_doc(LOG, doc.extraction_log)

	def test_a_result_lands_in_a_log_and_the_letter_waits_for_its_supplier(self):
		result = ExtractionResult(
			vendor_name="Nobody We Know AG",
			invoice_number="RE-2024-0042",
			invoice_date="2024-03-01",
			due_date="2024-03-31",
			currency="CHF",
			net_amount=100.0,
			vat_amount=8.1,
			gross_amount=108.1,
			confidence=0.87,
			raw={"model": "test"},
		)

		with _extractor_returning(result):
			sync_letters()

		log = self.log_of("inbox-1")
		self.assertEqual(log.kind, "Extraction")
		self.assertEqual(log.vendor_name, "Nobody We Know AG")
		self.assertEqual(log.invoice_number, "RE-2024-0042")
		self.assertEqual(log.invoice_date, datetime.date(2024, 3, 1))
		self.assertEqual(log.currency, "CHF")
		self.assertEqual(log.gross_amount, 108.1)
		self.assertEqual(log.confidence, 0.87)
		self.assertEqual(log.model, "test")

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Waiting for Supplier")
		self.assertIn("Nobody We Know AG", doc.processing_note)

	def test_a_letter_without_an_amount_waits_for_review(self):
		with _extractor_returning(ExtractionResult(invoice_number="RE-1")):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Needs Review")
		self.assertTrue(doc.processing_note)

	def test_a_currency_erpnext_does_not_know_is_dropped_not_saved(self):
		"""`currency` is a Link; an unknown code would fail the log's insert."""
		with _extractor_returning(ExtractionResult(currency="XYZ", gross_amount=10.0)):
			sync_letters()

		self.assertIsNone(self.log_of("inbox-1").currency)

	def test_an_unparseable_date_is_dropped_rather_than_losing_the_result(self):
		with _extractor_returning(ExtractionResult(invoice_number="RE-1", invoice_date="not a date")):
			sync_letters()

		log = self.log_of("inbox-1")
		self.assertIsNone(log.invoice_date)
		self.assertEqual(log.invoice_number, "RE-1")

	def test_an_extractor_returning_nothing_leaves_the_letter_at_downloaded(self):
		with _extractor_returning(None):
			summary = sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Downloaded")
		self.assertIsNone(doc.extraction_log)
		self.assertEqual(summary["letters_analyzed"], 0)

	def test_the_extractor_is_handed_the_bytes_that_were_downloaded(self):
		seen: dict[str, bytes] = {}

		with _extractor_recording(seen):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertTrue(seen[doc.letter_id].startswith(b"%PDF-"))
		# The id is in the PDF, so this proves the right letter's bytes arrived.
		self.assertIn(b"inbox-1", seen[doc.letter_id])

	def test_processing_again_by_hand_starts_over_from_extraction(self):
		calls: list[str] = []
		with _extractor_counting(calls):
			sync_letters()
			doc = self.letter_doc("inbox-1")
			self.assertEqual(doc.status, "Needs Review")
			before = len(calls)

			result = process(doc.name, force=1)

		self.assertEqual(len(calls), before + 1)
		self.assertEqual(result["status"], "Needs Review")

	def test_a_second_sync_does_not_re_extract_a_processed_letter(self):
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
