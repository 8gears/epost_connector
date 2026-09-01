"""The sync engine against a mock ePost, on a real site.

What matters here is that the sync converges. ePost is the source of truth and
`ePost Letter` rows are copies, so re-running has to leave the same rows, not a
second set of them, and it must not step on the fields a human owns. The
existing systems keep running alongside this one; a sync that drifts, duplicates
or overwrites is not a parallel-run sync.
"""

from __future__ import annotations

import contextlib
import inspect
import os
from unittest import mock

import frappe

from epost_connector.epost import sync as sync_module
from epost_connector.epost.sync import (
	LetterSync,
	_parse_datetime,
	_safe_name,
	_varchar_limit,
	reconcile,
	sync_letters,
)
from epost_connector.extraction import registry
from epost_connector.tests import mock_epost
from epost_connector.tests.mock_epost import (
	BAD_THUMBNAIL_LETTER,
	EMPTY_CONTENT_LETTER,
	HTML_CONTENT_LETTER,
	NFC_DIRECTORY_NAME,
	TRAVERSAL,
	TRAVERSAL_DATE_LETTER,
	TRAVERSAL_TITLE_LETTER,
	letter,
)
from epost_connector.tests.site_base import ePostSiteTestCase, registered_extractor

#: The two fixtures whose content endpoint answers 200 with something that is
#: not a PDF. Every other default fixture downloads cleanly.
UNDOWNLOADABLE = {HTML_CONTENT_LETTER, EMPTY_CONTENT_LETTER}


class SyncCoverageTest(ePostSiteTestCase):
	def test_every_letter_in_the_letterbox_becomes_a_row(self):
		summary = sync_letters()

		expected = {x["id"] for x in self.state.all_letters()}
		self.assertEqual(self.letter_ids(), expected)
		self.assertEqual(summary["letters_seen"], len(expected))
		self.assertEqual(summary["letters_new"], len(expected))

	def test_an_archived_letter_carries_the_folder_it_lives_in(self):
		"""A letter archived on ePost leaves the inbox listing entirely.

		Read from the inbox alone it would simply disappear from ERPNext, which
		is the one failure mode a copy of a letterbox must not have.
		"""
		sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").folder, "INBOX")
		self.assertEqual(self.letter_doc("arch-2").folder, "Storage")
		self.assertEqual(self.letter_doc("arch-1").folder, "Rechnungen")
		# Composed, not the decomposed form the service sent.
		self.assertEqual(self.letter_doc("arch-3").folder, NFC_DIRECTORY_NAME)

	def test_a_letter_added_between_runs_is_picked_up(self):
		sync_letters()
		self.state.inbox.append(letter("inbox-late", letterTitle="Arrived later"))

		summary = sync_letters()

		self.assertIn("inbox-late", self.letter_ids())
		self.assertEqual(summary["letters_new"], 1)

	def test_metadata_is_mapped_off_the_letter_payload(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")

		self.assertEqual(doc.title, "Gescannter Brief")
		self.assertEqual(doc.letter_type, "CLASSIC_LETTER")
		self.assertEqual(doc.epost_status, "UNREAD")
		self.assertEqual(doc.document_types, "invoice")
		self.assertIsNotNone(doc.received_at)
		self.assertEqual(frappe.parse_json(doc.raw_metadata)["id"], "inbox-1")

	def test_the_sender_falls_back_when_the_payload_carries_no_description(self):
		"""The `Letter` schema has no sender-name field at all (spec:16524)."""
		sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").sender_name, "Invoice from Muster Elektro AG")
		# inbox-2 has description None, so something else has to fill the column.
		fallback = self.letter_doc("inbox-2").sender_name
		self.assertTrue(fallback)
		self.assertEqual(fallback, "b0f742a3-0a54-401d-bf41-38a3a9628953")

	def test_the_sender_resolves_in_order_and_never_leaves_the_column_blank(self):
		"""description -> senderName -> senderParticipantId -> senderUserId.

		`senderName` is the odd rung: the schema does not document it, and it is
		read in case the live service returns more than it says. That branch is
		unreachable from the default fixtures, so it gets a payload of its own.
		"""
		self.state.content_override.clear()
		self.state.inbox = [
			letter("s-described", description="Invoice from Described AG", senderName="Ignored AG"),
			letter("s-named", description=None, senderName="Named AG"),
			letter("s-participant", description=None, senderParticipantId="participant-1"),
			letter("s-user", description=None, senderParticipantId=None, senderUserId="user-1"),
		]

		sync_letters()

		self.assertEqual(self.letter_doc("s-described").sender_name, "Invoice from Described AG")
		self.assertEqual(self.letter_doc("s-named").sender_name, "Named AG")
		self.assertEqual(self.letter_doc("s-participant").sender_name, "participant-1")
		self.assertEqual(self.letter_doc("s-user").sender_name, "user-1")

	def test_the_list_view_shows_only_columns_that_hold_a_real_value(self):
		"""List columns come only from `in_list_view`; there is no client API.

		`amount` is deliberately not among them, and this is the guard on putting
		it back. See `test_the_amount_column_would_be_fabricated_today` for why.
		"""
		meta = frappe.get_meta("ePost Letter")
		listed = [f.fieldname for f in meta.fields if f.in_list_view]

		self.assertEqual(listed, ["title", "sender_name", "received_at", "status", "folder"])

	def test_the_amount_column_would_be_fabricated_today(self):
		"""Why `amount` is not a list column, and the condition for adding it.

		A Currency column is `NOT NULL DEFAULT 0`, so an unextracted letter holds
		0 rather than nothing, and a Currency field with no `currency` beside it
		formats using the *site* default. Shown in a list that is read for
		bookkeeping, a letter nobody has opened therefore states an amount and a
		denomination that were never read off it.

		The app ships only the no-op extractor, so that is every row, always.
		Register a real extractor and this test starts failing — that is the
		signal to make `amount` a column.
		"""
		self.assertEqual(frappe.db.get_single_value("ePost Settings", "extractor"), "None")
		self.assertEqual(set(registry.EXTRACTORS), {"", "None"})

		self.state.content_override.clear()
		sync_letters()

		amounts = frappe.get_all("ePost Letter", pluck="amount")
		self.assertTrue(amounts)
		self.assertEqual(set(amounts), {0}, "an extractor now fills amount; reconsider the column")
		self.assertEqual(set(frappe.get_all("ePost Letter", pluck="currency")), {None})

	def test_a_currency_field_cannot_represent_an_amount_nobody_read(self):
		"""The reason the fix is to hide the field rather than to blank it.

		Both halves of the obvious fix are impossible, and this pins why so the
		next person does not spend the afternoon rediscovering it: the column is
		NOT NULL, and Frappe formats a null Currency exactly as it formats zero.
		"""
		from frappe.utils.formatters import format_value

		doc = frappe.get_doc(
			{"doctype": "ePost Letter", "letter_id": "probe-null-amount", "status": "New"}
		).insert(ignore_permissions=True)

		# The column is `decimal(21,9) NOT NULL DEFAULT 0`, so "no amount" cannot
		# be stored as anything but zero.
		with self.assertRaises(Exception):
			frappe.db.set_value("ePost Letter", doc.name, "amount", None, update_modified=False)

		# And even if it could be, it would render identically to zero anyway.
		field = frappe.get_meta("ePost Letter").get_field("amount")
		self.assertEqual(format_value(None, df=field), format_value(0, df=field))

	def test_the_extraction_section_is_hidden_until_it_holds_something(self):
		"""Otherwise an unread letter shows `0.00` under a site-default symbol.

		Every field in the section is falsy before extraction — the Currency ones
		read 0, which is falsy in the same way an empty Data field is — so one
		predicate over all of them hides the section exactly when it is empty.
		"""
		section = frappe.get_meta("ePost Letter").get_field("extraction_section")
		self.assertTrue(section.depends_on)

		self.state.content_override.clear()
		sync_letters()
		doc = self.letter_doc("inbox-1")

		for fieldname in (
			"vendor_name",
			"invoice_number",
			"invoice_date",
			"due_date",
			"currency",
			"amount",
			"vat_amount",
		):
			with self.subTest(field=fieldname):
				self.assertIn(fieldname, section.depends_on)
				self.assertFalse(doc.get(fieldname), "a truthy value here would show the empty section")

	def test_the_section_reappears_once_an_extractor_has_read_something(self):
		self.state.content_override.clear()

		with registered_extractor("Fixed", _extractor_returning_amount()):
			sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Analyzed")
		self.assertTrue(doc.amount, "the predicate would still hide a section that now has data")
		self.assertEqual(doc.currency, "CHF")

	def test_the_amount_reads_its_symbol_from_the_currency_beside_it(self):
		"""So when a real extractor does fill both, no separate currency column
		is needed — which is why `currency` is not one either."""
		meta = frappe.get_meta("ePost Letter")

		self.assertEqual(meta.get_field("amount").options, "currency")
		self.assertEqual(meta.get_field("vat_amount").options, "currency")

	def test_the_folder_is_reachable_as_a_filter_as_well_as_a_column(self):
		self.assertTrue(frappe.get_meta("ePost Letter").get_field("folder").in_standard_filter)

	def test_the_letter_list_sorts_on_when_the_letter_arrived(self):
		"""v16 accepts a non-standard `sort_field`; it was flagged as a risk."""
		meta = frappe.get_meta("ePost Letter")

		self.assertEqual(meta.sort_field, "received_at")
		self.assertEqual(meta.sort_order, "DESC")


class IdempotencyTest(ePostSiteTestCase):
	def test_two_runs_converge_on_the_same_rows(self):
		first = sync_letters()
		names_after_first = set(frappe.get_all("ePost Letter", pluck="name"))

		second = sync_letters()

		self.assertEqual(set(frappe.get_all("ePost Letter", pluck="name")), names_after_first)
		self.assertEqual(second["letters_seen"], first["letters_seen"])
		self.assertEqual(second["letters_new"], 0)

	def test_a_second_run_does_not_download_the_pdf_again(self):
		sync_letters()
		self.state.calls.clear()

		summary = sync_letters()

		self.assertEqual(summary["files_downloaded"], 0)
		content_calls = [c for c in self.state.calls if c[1].endswith("/content")]
		# Only the two whose content endpoint refuses to produce a PDF, because
		# they have no file to skip on.
		self.assertEqual({c[1].split("/")[-2] for c in content_calls}, UNDOWNLOADABLE)

	def test_the_letter_id_is_unique_so_a_duplicate_cannot_be_inserted(self):
		"""Convergence must not rest on the sync remembering to check.

		`autoname: field:letter_id` makes the id the primary key, so two workers
		racing on the same letter lose to the database rather than to each other.
		"""
		sync_letters()

		with self.assertRaises(frappe.exceptions.DuplicateEntryError):
			frappe.get_doc({"doctype": "ePost Letter", "letter_id": "inbox-1", "status": "New"}).insert(
				ignore_permissions=True
			)

	def test_a_field_the_user_owns_survives_a_re_sync(self):
		"""The sync writes ePost's facts. Everything else on the row is a human's."""
		sync_letters()
		doc = self.letter_doc("inbox-1")

		supplier = _make_supplier("ePost Test Supplier")
		doc.supplier = supplier
		doc.save(ignore_permissions=True)
		doc.add_tag("needs-review")
		frappe.db.commit()

		sync_letters()

		refreshed = self.letter_doc("inbox-1")
		self.assertEqual(refreshed.supplier, supplier)
		self.assertIn("needs-review", refreshed._user_tags or "")

	def test_a_terminal_status_is_not_walked_back(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")
		doc.status = "Ignored"
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		sync_letters()

		self.assertEqual(self.letter_doc("inbox-1").status, "Ignored")


class StatusTransitionTest(ePostSiteTestCase):
	def test_a_downloaded_letter_reaches_downloaded_and_carries_its_pdf(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")

		self.assertEqual(doc.status, "Downloaded")
		self.assertTrue(doc.file)
		self.assertEqual(len(doc.content_sha256), 64)
		self.assertTrue(frappe.db.exists("File", {"file_url": doc.file}))

	def test_the_status_only_ever_moves_forward(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")
		doc.status = "Analyzed"
		doc.save(ignore_permissions=True)

		doc.status = "New"
		with self.assertRaises(frappe.exceptions.ValidationError):
			doc.save(ignore_permissions=True)

	def test_ignoring_a_letter_is_allowed_from_any_state(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")
		doc.status = "Analyzed"
		doc.save(ignore_permissions=True)

		doc.status = "Ignored"
		doc.save(ignore_permissions=True)

		self.assertEqual(self.letter_doc("inbox-1").status, "Ignored")

	def test_a_letter_whose_invoice_is_gone_can_come_back_into_the_queue(self):
		sync_letters()
		doc = self.letter_doc("inbox-1")
		doc.status = "Imported"
		doc.save(ignore_permissions=True)

		# The Purchase Invoice was deleted, so the link is dangling.
		doc.status = "Downloaded"
		doc.save(ignore_permissions=True)

		self.assertEqual(self.letter_doc("inbox-1").status, "Downloaded")


class BadContentTest(ePostSiteTestCase):
	"""A 200 from the content endpoint is not proof of a PDF."""

	def test_a_letter_whose_pdf_will_not_serve_is_still_a_row(self):
		sync_letters()

		for letter_id in UNDOWNLOADABLE:
			with self.subTest(letter=letter_id):
				doc = self.letter_doc(letter_id)
				self.assertEqual(doc.status, "New")
				self.assertFalse(doc.file)
				self.assertTrue(doc.sync_error, "the reason must be recorded on the row")

	def test_neither_bad_answer_is_stored_as_a_pdf(self):
		sync_letters()

		for letter_id in UNDOWNLOADABLE:
			with self.subTest(letter=letter_id):
				self.assertEqual(frappe.get_all("File", filters={"attached_to_name": letter_id}), [])

	def test_the_run_reports_partial_rather_than_success(self):
		summary = sync_letters()

		self.assertEqual(summary["status"], "Partial")
		self.assertEqual(summary["errors"], len(UNDOWNLOADABLE))

	def test_one_bad_letter_costs_exactly_one_letter(self):
		summary = sync_letters()

		good = {x["id"] for x in self.state.all_letters()} - UNDOWNLOADABLE
		self.assertEqual(summary["files_downloaded"], len(good))
		for letter_id in good:
			self.assertEqual(self.letter_doc(letter_id).status, "Downloaded")

	def test_the_error_clears_once_the_gateway_recovers(self):
		sync_letters()
		self.state.content_override.clear()

		sync_letters()

		for letter_id in UNDOWNLOADABLE:
			with self.subTest(letter=letter_id):
				doc = self.letter_doc(letter_id)
				self.assertEqual(doc.status, "Downloaded")
				self.assertFalse(doc.sync_error)


class AttachmentMechanicsTest(ePostSiteTestCase):
	"""Framework behaviour `download` is written around, pinned by measurement.

	The code carries a `letter.reload()` between inserting the File and saving
	the letter, and used to explain it as a guard against Frappe writing
	`attached_to_field` back onto the parent and bumping its `modified`. On
	Frappe 16 that does not happen, so the guard is against a different risk —
	hooks this app does not control — and the test below is the canary: if a
	future version starts writing the field back, this fails and says so rather
	than the sync starting to raise TimestampMismatchError in production.
	"""

	def test_frappe_does_not_write_attached_to_field_back_onto_the_parent(self):
		letter_doc = frappe.get_doc(
			{"doctype": "ePost Letter", "letter_id": "probe-attach", "status": "New"}
		).insert(ignore_permissions=True)
		before = frappe.db.get_value("ePost Letter", letter_doc.name, "modified")

		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "probe.pdf",
				"attached_to_doctype": "ePost Letter",
				"attached_to_name": letter_doc.name,
				"attached_to_field": "file",
				"is_private": 1,
				"content": mock_epost.minimal_pdf("probe"),
			}
		).insert(ignore_permissions=True)

		message = (
			"Frappe now writes attached_to_field back onto the parent. The reload in "
			"LetterSync.download is load-bearing again — restore the comment explaining that, and "
			"check every other place this app inserts a File with attached_to_field set."
		)
		self.assertIsNone(frappe.db.get_value("ePost Letter", letter_doc.name, "file"), message)
		self.assertEqual(frappe.db.get_value("ePost Letter", letter_doc.name, "modified"), before, message)

	def test_the_download_survives_that_timestamp_bump(self):
		"""The end-to-end proof: no TimestampMismatchError anywhere in a sync."""
		self.state.content_override.clear()

		summary = sync_letters()

		self.assertEqual(summary["status"], "Success")
		self.assertEqual(summary["files_downloaded"], len(self.state.all_letters()))

	def test_the_pdf_and_the_thumbnail_are_both_attached_to_the_letter(self):
		self.state.content_override.clear()
		sync_letters()
		doc = self.letter_doc("inbox-1")

		attached = frappe.get_all(
			"File", filters={"attached_to_name": doc.name}, fields=["file_url", "attached_to_field"]
		)
		self.assertEqual(
			{a.attached_to_field for a in attached},
			{"file", "thumbnail"},
		)
		self.assertEqual(len(attached), 2)


class ThumbnailTest(ePostSiteTestCase):
	"""The preview is cosmetic, and nothing may be lost over one."""

	def test_a_good_letter_gets_its_thumbnail(self):
		sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertTrue(doc.thumbnail)
		self.assertTrue(frappe.db.exists("File", {"file_url": doc.thumbnail}))

	def test_an_undecodable_preview_does_not_cost_the_letter_its_pdf(self):
		"""The decode fails inside Frappe's File insert, long after the download.

		Sharing the download's savepoint, that failure rolled the PDF, the hash
		and the status back — on every run, so the letter never left New while
		its content downloaded correctly each time.
		"""
		sync_letters()

		doc = self.letter_doc(BAD_THUMBNAIL_LETTER)
		self.assertEqual(doc.status, "Downloaded")
		self.assertTrue(doc.file)
		self.assertEqual(len(doc.content_sha256), 64)
		self.assertFalse(doc.thumbnail)

	def test_no_half_attachment_survives_the_failed_preview(self):
		sync_letters()

		attached = frappe.get_all(
			"File", filters={"attached_to_name": BAD_THUMBNAIL_LETTER}, pluck="file_name"
		)
		self.assertEqual(len(attached), 1, f"only the PDF should be attached, found {attached}")
		self.assertTrue(attached[0].endswith(".pdf"))

	def test_a_thumbnail_failure_is_not_counted_as_a_letter_error(self):
		summary = sync_letters()

		self.assertNotIn(BAD_THUMBNAIL_LETTER, "\n".join(_errors_of_last_run()))
		self.assertEqual(summary["errors"], len(UNDOWNLOADABLE))

	def test_an_extractor_that_throws_does_not_cost_the_letter_its_pdf(self):
		"""Same shape as the thumbnail case, one stage later in the pipeline."""
		self.state.content_override.clear()

		with _extractor_that_raises():
			summary = sync_letters()

		doc = self.letter_doc("inbox-1")
		self.assertEqual(doc.status, "Downloaded")
		self.assertTrue(doc.file)
		self.assertEqual(summary["files_downloaded"], len(self.state.all_letters()))
		self.assertEqual(summary["status"], "Partial")


class HostileInputTest(ePostSiteTestCase):
	"""Service-supplied strings decide nothing about the filesystem."""

	def test_a_path_shaped_title_cannot_decide_where_the_file_lands(self):
		"""Frappe builds the stored path out of `file_name`.

		What makes traversal impossible is the absence of a separator, not the
		absence of dots — `..` with nothing to separate it is just characters in
		a name — so the invariant asserted last is where the bytes actually
		landed.
		"""
		sync_letters()
		doc = self.letter_doc(TRAVERSAL_TITLE_LETTER)

		file_doc = frappe.get_doc("File", {"file_url": doc.file})
		self.assertNotIn("/", file_doc.file_name)
		self.assertNotIn("\\", file_doc.file_name)
		self.assertFalse(file_doc.file_name.startswith("."))
		self.assertTrue(doc.file.startswith("/private/files/"))

		private_files = os.path.realpath(frappe.get_site_path("private", "files"))
		self.assertEqual(
			os.path.commonpath([private_files, os.path.realpath(file_doc.get_full_path())]), private_files
		)

	def test_a_path_shaped_date_is_dropped_rather_than_stored(self):
		sync_letters()
		doc = self.letter_doc(TRAVERSAL_DATE_LETTER)

		self.assertIsNone(doc.received_at)
		# The row still exists and still downloaded: an unparseable date is not
		# a reason to lose the letter.
		self.assertEqual(doc.status, "Downloaded")

	def test_the_sanitiser_leaves_nothing_a_path_can_be_built_from(self):
		for hostile in (TRAVERSAL, "/etc/passwd", "..", "...", "a/b\\c", "", None):
			with self.subTest(value=hostile):
				safe = _safe_name(hostile)
				self.assertTrue(safe)
				self.assertNotIn("/", safe)
				self.assertNotIn("\\", safe)
				self.assertFalse(safe.startswith("."))

	def test_a_letter_without_an_id_is_reported_not_dropped(self):
		"""It cannot be stored — the id is the row's identity — but it must be said.

		Skipping it inside the client's dedup left a letterbox with a letter in
		it that ERPNext did not have, and a run that reported Success.
		"""
		self.state.inbox = [letter("inbox-1"), {"letterTitle": "no id at all"}]
		self.state.content_override.clear()

		summary = sync_letters()

		self.assertEqual(self.letter_ids(), {"inbox-1", "arch-1", "arch-2", "arch-3"})
		self.assertEqual(summary["status"], "Partial")
		self.assertEqual(summary["errors"], 1)
		self.assertIn("without an id", "\n".join(_errors_of_last_run()))


class LongValueTest(ePostSiteTestCase):
	"""A service string longer than its column must not cost the whole letter.

	Frappe raises CharacterLengthExceededError rather than truncating, and the
	per-letter guard turned that into no row at all — so the letter was absent
	from ERPNext, could not carry its own `sync_error` because the row the
	reason belongs on was never created, and was re-attempted and re-failed on
	every hourly run.
	"""

	LONG = "Rechnung " + ("sehr lang " * 30)  # 309 characters

	def setUp(self) -> None:
		super().setUp()
		self.state.content_override.clear()

	def test_an_over_long_title_still_produces_a_complete_letter(self):
		self.state.inbox = [letter("long-title", letterTitle=self.LONG)]

		summary = sync_letters()

		doc = self.letter_doc("long-title")
		self.assertEqual(summary["status"], "Success")
		self.assertEqual(summary["errors"], 0)
		self.assertEqual(doc.status, "Downloaded")
		self.assertTrue(doc.file)

	def test_the_value_is_cut_to_the_width_of_its_column(self):
		self.state.inbox = [letter("long-title", letterTitle=self.LONG)]

		sync_letters()

		doc = self.letter_doc("long-title")
		limit = _varchar_limit(frappe.get_meta("ePost Letter").get_field("title"))
		self.assertEqual(len(doc.title), limit)
		self.assertTrue(self.LONG.startswith(doc.title))

	def test_nothing_is_actually_lost_because_the_payload_is_kept_whole(self):
		self.state.inbox = [letter("long-title", letterTitle=self.LONG)]

		sync_letters()

		payload = frappe.parse_json(self.letter_doc("long-title").raw_metadata)
		self.assertEqual(payload["letterTitle"], self.LONG)

	def test_an_over_long_sender_and_folder_are_cut_too(self):
		"""Three separate Data columns, all filled from service strings."""
		self.state.inbox = [letter("long-sender", description=self.LONG)]
		self.state.directories = [
			{"directoryId": "dir-long", "directoryName": self.LONG, "numberOfDocuments": 1}
		]
		self.state.in_folder = {"dir-long": ["arch-1"]}

		summary = sync_letters()

		self.assertEqual(summary["errors"], 0)
		self.assertEqual(len(self.letter_doc("long-sender").sender_name), 140)
		self.assertEqual(len(self.letter_doc("arch-1").folder), 140)

	def test_the_limit_is_read_from_the_field_rather_than_assumed(self):
		"""`content_sha256` declares length 64; a hard-coded 140 would miss it."""
		meta = frappe.get_meta("ePost Letter")

		self.assertEqual(_varchar_limit(meta.get_field("content_sha256")), 64)
		self.assertEqual(_varchar_limit(meta.get_field("title")), 140)
		# Not varchar-backed, so not clamped at all.
		self.assertEqual(_varchar_limit(meta.get_field("raw_metadata")), 0)
		self.assertEqual(_varchar_limit(meta.get_field("received_at")), 0)


class ConcurrentSyncTest(ePostSiteTestCase):
	"""Two syncs at once attach the letter's PDF twice.

	`download` checks for an existing File and then creates one, which is a race
	whenever a Desk button press overlaps the hourly job. The letter row is safe
	— `letter_id` is the primary key — and the bytes are not duplicated on disk,
	but the attachment list grows by one per overlap.
	"""

	def test_the_sync_job_id_is_stable_so_frappe_can_refuse_a_second_one(self):
		"""A random id per press made every enqueue unique to Frappe."""
		self.assertEqual(sync_module.SYNC_JOB_ID, "epost-sync")

		source = inspect.getsource(sync_module.run_sync_now)
		self.assertIn("deduplicate=True", source)
		self.assertNotIn("generate_hash", source)

	def test_a_press_while_a_sync_is_queued_does_not_queue_a_second(self):
		with mock.patch.object(sync_module, "is_job_enqueued", return_value=True) as enqueued:
			with mock.patch.object(frappe, "enqueue") as enqueue:
				result = sync_module.run_sync_now()

		enqueued.assert_called_once_with(sync_module.SYNC_JOB_ID)
		enqueue.assert_not_called()
		self.assertTrue(result["already_running"])

	def test_a_press_with_nothing_queued_enqueues_under_the_shared_id(self):
		with mock.patch.object(sync_module, "is_job_enqueued", return_value=False):
			with mock.patch.object(frappe, "enqueue") as enqueue:
				sync_module.run_sync_now()

		self.assertEqual(enqueue.call_args.kwargs["job_id"], sync_module.SYNC_JOB_ID)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_a_second_download_on_a_stale_doc_still_attaches_only_once(self):
		"""The residual race, pinned so a regression is visible.

		Two workers each holding a copy of the letter from before either wrote
		`file` both see an empty field. Frappe deduplicates the bytes, so this
		costs an extra File row and not an extra document.
		"""
		self.state.content_override.clear()
		sync_letters()

		stale = frappe.get_doc("ePost Letter", {"letter_id": "inbox-1"})
		stale.file = None
		LetterSync().download(stale)

		rows = frappe.get_all("File", filters={"attached_to_name": "inbox-1", "attached_to_field": "file"})
		self.assertEqual(
			len(rows),
			2,
			"the check-then-create race is closed; update this test and the note in "
			"ConcurrentSyncTest's docstring",
		)
		self.assertEqual(len({frappe.db.get_value("File", r.name, "file_url") for r in rows}), 1)


class SyncLogTest(ePostSiteTestCase):
	def test_a_run_writes_exactly_one_log_row(self):
		sync_letters()

		logs = frappe.get_all("ePost Sync Log", fields=["name", "status", "letters_seen", "errors"])
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Partial")
		self.assertEqual(logs[0].letters_seen, len(self.state.all_letters()))
		self.assertTrue(logs[0].errors)

	def test_two_runs_write_two_log_rows(self):
		sync_letters()
		sync_letters()

		self.assertEqual(frappe.db.count("ePost Sync Log"), 2)

	def test_a_clean_run_is_logged_as_success(self):
		self.state.content_override.clear()

		summary = sync_letters()

		self.assertEqual(summary["status"], "Success")
		log = frappe.get_all("ePost Sync Log", fields=["status", "errors", "files_downloaded"])[0]
		self.assertEqual(log.status, "Success")
		self.assertIsNone(log.errors)
		self.assertEqual(log.files_downloaded, len(self.state.all_letters()))

	def test_the_settings_record_when_the_last_run_finished(self):
		sync_letters()

		self.assertIsNotNone(frappe.db.get_single_value("ePost Settings", "last_sync_at"))
		self.assertIn("Partial", frappe.db.get_single_value("ePost Settings", "last_sync_status"))

	def test_a_run_that_cannot_reach_epost_is_logged_as_failed(self):
		self.state.force_status = [{"status": 500} for _ in range(20)]
		client = LetterSync().client
		client.BACKOFF_SECONDS = 0

		summary = LetterSync(client=client).run()

		self.assertEqual(summary["status"], "Failed")
		self.assertEqual(frappe.get_all("ePost Sync Log", pluck="status"), ["Failed"])


class CredentialRedactionTest(ePostSiteTestCase):
	"""What a run stores is read by people who do not hold the credentials.

	An upstream error body can be built out of the request that produced it — a
	proxy quoting a header, a gateway quoting the form it rejected — and that
	text is written to the Sync Log, to the letter's own `sync_error` and to
	Error Log. All three outlive the run.
	"""

	def test_an_echoed_api_key_reaches_none_of_the_places_a_run_writes_to(self):
		self.configure_settings(api_key=mock_epost.API_KEY, username=None, password=None)
		self.state.inbox = [letter(mock_epost.ECHO_SECRET_LETTER)]
		self.state.archive = []

		summary = LetterSync().run()

		self.assertEqual(summary["status"], "Partial")
		errors = "\n".join(_errors_of_last_run())
		self.assertIn("[redacted]", errors)
		for stored in (errors, self.letter_doc(mock_epost.ECHO_SECRET_LETTER).sync_error, _error_log_text()):
			self.assertNotIn(mock_epost.API_KEY, stored or "")


class TruncatedListingTest(ePostSiteTestCase):
	"""A service that ignores `offset` gives a partial run, not a clean one."""

	def test_the_ceiling_is_reported_and_what_was_read_is_kept(self):
		self.state.inbox = [letter(f"p-{i}") for i in range(25)]
		self.state.content_override.clear()
		self.state.ignore_offset = True

		summary = LetterSync(client=_client_with_page_size(10)).run()

		self.assertEqual(summary["status"], "Partial")
		self.assertEqual(len(self.letter_ids() & {f"p-{i}" for i in range(25)}), 10)
		log_errors = frappe.get_all("ePost Sync Log", pluck="errors")[0]
		self.assertIn("ignoring `offset`", log_errors)


class ReconcileTest(ePostSiteTestCase):
	def test_a_synced_letterbox_reconciles_clean(self):
		sync_letters()

		result = reconcile()

		self.assertTrue(result["in_sync"], result)
		self.assertEqual(result["epost_letters"], len(self.state.all_letters()))
		self.assertEqual(result["erpnext_letters"], len(self.state.all_letters()))
		self.assertEqual(result["missing_in_erpnext_count"], 0)
		self.assertEqual(result["extra_in_erpnext_count"], 0)

	def test_a_letter_that_never_synced_is_named_as_missing(self):
		sync_letters()
		self.state.inbox.append(letter("inbox-unsynced"))

		result = reconcile()

		self.assertFalse(result["in_sync"])
		self.assertEqual(result["missing_in_erpnext"], ["inbox-unsynced"])
		self.assertEqual(result["extra_in_erpnext_count"], 0)

	def test_a_row_with_no_letter_behind_it_is_named_as_extra(self):
		sync_letters()
		frappe.get_doc({"doctype": "ePost Letter", "letter_id": "ghost", "status": "New"}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()

		result = reconcile()

		self.assertFalse(result["in_sync"])
		self.assertEqual(result["extra_in_erpnext"], ["ghost"])

	def test_a_truncated_listing_is_never_reported_as_in_sync(self):
		"""Comparing against a partial listing would invent a clean diff."""
		self.state.inbox = [letter(f"p-{i}") for i in range(25)]
		self.state.content_override.clear()
		self.state.ignore_offset = True

		result = _reconcile_with_page_size(10)

		self.assertFalse(result["in_sync"])
		self.assertIn("ignoring `offset`", result["listing_truncated"])

	def test_the_count_is_not_capped_by_a_default_page_length(self):
		"""`frappe.get_all` returning only the first 20 rows would fake a match."""
		self.state.inbox = [letter(f"p-{i}") for i in range(40)]
		self.state.content_override.clear()
		sync_letters()

		result = reconcile()

		self.assertEqual(result["erpnext_letters"], 43)
		self.assertTrue(result["in_sync"], result)


class ScheduledSyncTest(ePostSiteTestCase):
	def test_the_hourly_entry_point_does_nothing_while_disabled(self):
		self.configure_settings(enabled=0)

		self.assertIsNone(sync_module.scheduled_sync())
		self.assertEqual(frappe.db.count("ePost Letter"), 0)
		self.assertEqual(frappe.db.count("ePost Sync Log"), 0)

	def test_the_hourly_entry_point_runs_while_enabled(self):
		summary = sync_module.scheduled_sync()

		self.assertIsNotNone(summary)
		self.assertEqual(summary["letters_seen"], len(self.state.all_letters()))

	def test_the_hook_registers_the_entry_point_that_exists(self):
		hooks = frappe.get_hooks("scheduler_events", app_name="epost_connector")
		targets = hooks.get("hourly_long", [])

		self.assertEqual(targets, ["epost_connector.epost.sync.scheduled_sync"])
		self.assertTrue(callable(frappe.get_attr(targets[0])))


class DatetimeParsingTest(ePostSiteTestCase):
	def test_a_utc_timestamp_lands_in_the_site_timezone(self):
		parsed = _parse_datetime("2021-09-29T04:21:10.163Z")

		self.assertIsNotNone(parsed)
		self.assertIsNone(parsed.tzinfo, "Frappe stores naive datetimes")

	def test_junk_is_dropped_rather_than_raised(self):
		for value in (TRAVERSAL, "", None, "not-a-date", "2021-13-45T99:99:99Z"):
			with self.subTest(value=value):
				self.assertIsNone(_parse_datetime(value))


def _extractor_returning_amount():
	"""An extractor that fills the two fields the hidden section turns on."""
	from epost_connector.extraction.base import ExtractionResult, LetterExtractor

	class Fixed(LetterExtractor):
		name = "Fixed"

		def extract(self, letter_doc, pdf_bytes):
			return ExtractionResult(gross_amount=96.64, currency="CHF")

	return Fixed


def _error_log_text() -> str:
	rows = frappe.get_all("Error Log", fields=["method", "error"])
	return "\n".join(f"{row.method}\n{row.error}" for row in rows)


def _errors_of_last_run() -> list[str]:
	rows = frappe.get_all("ePost Sync Log", fields=["errors"], order_by="creation desc", limit=1)
	return (rows[0].errors or "").splitlines() if rows else []


def _extractor_that_raises():
	from epost_connector.extraction.base import LetterExtractor

	class Exploding(LetterExtractor):
		name = "Exploding"

		def extract(self, letter_doc, pdf_bytes):
			raise RuntimeError("the extractor could not read this PDF")

	return registered_extractor("Exploding", Exploding)


def _client_with_page_size(page_size: int):
	from epost_connector.epost.client import ePostClient

	client = ePostClient.from_settings(page_size=page_size)
	client.BACKOFF_SECONDS = 0
	return client


def _reconcile_with_page_size(page_size: int) -> dict:
	"""`reconcile()` builds its own client, so the page size goes in by patch."""
	from epost_connector.epost.client import ePostClient

	original = ePostClient.from_settings
	try:
		ePostClient.from_settings = classmethod(
			lambda cls, settings=None, **kw: original.__func__(cls, settings, page_size=page_size, **kw)
		)
		return reconcile()
	finally:
		ePostClient.from_settings = original


def _make_supplier(name: str) -> str:
	if frappe.db.exists("Supplier", name):
		return name
	supplier = frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": name, "supplier_group": _supplier_group()}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return supplier.name


def _supplier_group() -> str:
	existing = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
	if existing:
		return existing
	group = frappe.get_doc(
		{"doctype": "Supplier Group", "supplier_group_name": "ePost Test Group", "is_group": 0}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return group.name
