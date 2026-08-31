"""One-way sync: ePost letterbox -> `ePost Letter` rows.

ePost is the source of truth. Rows here are copies, keyed on the ePost letter
id, so re-running converges instead of duplicating. Nothing is ever written back
to ePost (see `client.py` for the read-only contract).
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import frappe
from frappe.utils import now_datetime
from frappe.utils.data import convert_utc_to_system_timezone

from epost_connector.epost.client import ePostClient
from epost_connector.epost.exceptions import ePostError, ePostPaginationLimit
from epost_connector.extraction.pipeline import analyze_letter

DOCTYPE = "ePost Letter"

#: Status only ever moves forward. Imported/Ignored are terminal: the sync
#: refreshes their ePost metadata but does not touch their pipeline state.
STATUS_RANK = {"New": 0, "Downloaded": 1, "Analyzed": 2, "Imported": 3, "Ignored": 3}
TERMINAL_STATUSES = {"Imported", "Ignored"}


def scheduled_sync() -> dict | None:
	"""Hourly entry point registered in hooks.py."""
	if not frappe.db.get_single_value("ePost Settings", "enabled"):
		return None
	return sync_letters()


def sync_letters() -> dict:
	"""Pull every letter, inbox and archive, download PDFs, run extraction."""
	return LetterSync().run()


@frappe.whitelist()
def run_sync_now() -> dict:
	"""Desk button: enqueue a sync so the request returns immediately."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	job = frappe.enqueue(
		"epost_connector.epost.sync.sync_letters",
		queue="long",
		timeout=1800,
		job_id=f"epost-sync-{frappe.generate_hash(length=8)}",
	)
	return {"job_id": getattr(job, "id", None)}


@frappe.whitelist()
def reconcile() -> dict:
	"""Compare the ePost letterbox against the `ePost Letter` table.

	Run from the shell with:
	    bench --site <site> execute epost_connector.epost.sync.reconcile
	"""
	frappe.only_for(("System Manager", "Accounts Manager"))

	client = ePostClient.from_settings()
	remote_ids: set[str] = set()
	truncated = None

	try:
		for payload, _folder in client.iter_all_letters():
			if payload.get("id"):
				remote_ids.add(str(payload["id"]))
	except ePostPaginationLimit as exc:
		# Report the ceiling instead of silently comparing against a partial
		# listing, which would invent a clean diff out of missing data.
		truncated = str(exc)

	local_ids = set(frappe.get_all(DOCTYPE, pluck="letter_id"))
	missing = sorted(remote_ids - local_ids)
	extra = sorted(local_ids - remote_ids)

	return {
		"epost_letters": len(remote_ids),
		"erpnext_letters": len(local_ids),
		"in_both": len(remote_ids & local_ids),
		"missing_in_erpnext": missing,
		"missing_in_erpnext_count": len(missing),
		"extra_in_erpnext": extra,
		"extra_in_erpnext_count": len(extra),
		"listing_truncated": truncated,
		"in_sync": not missing and not extra and not truncated,
	}


class LetterSync:
	"""One sync run, recorded as one `ePost Sync Log` row."""

	def __init__(self, client: ePostClient | None = None) -> None:
		self.settings = frappe.get_cached_doc("ePost Settings")
		self.client = client or ePostClient.from_settings(self.settings)
		self.errors: list[str] = []
		self.seen = 0
		self.created = 0
		self.downloaded = 0
		self.analyzed = 0

	def run(self) -> dict:
		log = self._start_log()
		try:
			for payload, folder in self.client.iter_all_letters():
				self.seen += 1
				self._sync_one(payload, folder)
		except ePostPaginationLimit as exc:
			# The letters inside the window were still synced, so this is a
			# partial run with a known ceiling, not a failure.
			self.errors.append(str(exc))
			return self._finish(log, "Partial")
		except ePostError as exc:
			self.errors.append(f"Sync aborted: {exc}")
			return self._finish(log, "Failed")
		except Exception as exc:
			self.errors.append(f"Sync aborted: {exc}")
			frappe.log_error(title="ePost sync failed", message=frappe.get_traceback())
			return self._finish(log, "Failed")

		return self._finish(log, "Partial" if self.errors else "Success")

	def _sync_one(self, payload: dict, folder: str) -> None:
		letter_id = str(payload.get("id") or "").strip()
		if not letter_id:
			self.errors.append(f"Letter without an id skipped: {frappe.as_json(payload)[:200]}")
			return

		# Metadata and content are committed separately: a letter whose PDF the
		# gateway will not serve is still worth having as a row, with the reason
		# recorded on it.
		letter = self._guarded(letter_id, lambda: self._upsert(letter_id, payload, folder))
		if letter is None or letter.status in TERMINAL_STATUSES:
			return

		self._guarded(letter_id, lambda: self._fetch_and_analyze(letter))

	def _fetch_and_analyze(self, letter) -> None:
		self.download(letter)
		self._analyze(letter)

	def _guarded(self, letter_id: str, action):
		"""Run `action` in its own savepoint. One bad letter costs one letter."""
		savepoint = f"epost_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		try:
			result = action()
			frappe.db.commit()
			return result
		except Exception as exc:
			frappe.db.rollback(save_point=savepoint)
			self.errors.append(f"{letter_id}: {exc}")
			self._record_letter_error(letter_id, exc)
			return None

	def _upsert(self, letter_id: str, payload: dict, folder: str):
		values = self._metadata(payload, folder)
		name = frappe.db.get_value(DOCTYPE, {"letter_id": letter_id}, "name")

		if not name:
			letter = frappe.get_doc({"doctype": DOCTYPE, "letter_id": letter_id, "status": "New", **values})
			letter.insert(ignore_permissions=True)
			self.created += 1
			return letter

		letter = frappe.get_doc(DOCTYPE, name)
		for field, value in values.items():
			letter.set(field, value)
		if letter.sync_error:
			letter.sync_error = None
		letter.save(ignore_permissions=True)
		return letter

	def download(self, letter) -> None:
		"""Fetch the PDF unless this letter already has one on disk."""
		if letter.file and frappe.db.exists("File", {"file_url": letter.file}):
			return

		content = self.client.get_letter_content(letter.letter_id)
		file_doc = self._attach(letter, self._pdf_filename(letter), content)

		# Inserting a File with `attached_to_field` writes that field on the
		# parent and bumps its `modified`, so saving the stale in-memory doc
		# would raise TimestampMismatchError.
		letter.reload()

		letter.file = file_doc.file_url
		letter.content_sha256 = hashlib.sha256(content).hexdigest()
		self._advance(letter, "Downloaded")
		letter.save(ignore_permissions=True)
		self.downloaded += 1

		self._download_thumbnail(letter)

	def _download_thumbnail(self, letter) -> None:
		if letter.thumbnail:
			return
		try:
			content = self.client.get_letter_thumbnail(letter.letter_id)
		except ePostError:
			return  # Thumbnails are cosmetic; never fail a sync over one.
		if not content:
			return
		file_doc = self._attach(
			letter, f"{_safe_name(letter.letter_id)}-thumb.jpg", content, field="thumbnail"
		)
		letter.db_set("thumbnail", file_doc.file_url, update_modified=False)

	def _analyze(self, letter) -> None:
		if analyze_letter(letter):
			self.analyzed += 1

	@staticmethod
	def _attach(letter, filename: str, content: bytes, field: str = "file"):
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"attached_to_doctype": DOCTYPE,
				"attached_to_name": letter.name,
				"attached_to_field": field,
				"is_private": 1,
				"content": content,
			}
		).insert(ignore_permissions=True)

	@staticmethod
	def _pdf_filename(letter) -> str:
		"""Derive a file name from the letter id and title, never from raw
		service strings: a value shaped like `../..` would otherwise decide
		where the file lands."""
		stem = _safe_name(letter.letter_id)
		title = _safe_name(letter.title)[:60].strip("_")
		return f"{stem}-{title}.pdf" if title else f"{stem}.pdf"

	@staticmethod
	def _advance(letter, status: str) -> None:
		if STATUS_RANK.get(status, 0) > STATUS_RANK.get(letter.status, 0):
			letter.status = status

	def _metadata(self, payload: dict, folder: str) -> dict:
		document_types = payload.get("documentTypes") or []
		return {
			"title": payload.get("letterTitle") or payload.get("fileName") or payload.get("id"),
			"sender_name": self._sender_name(payload),
			"received_at": _parse_datetime(payload.get("receivedDateTime")),
			"document_types": ", ".join(str(t) for t in document_types) if document_types else None,
			"epost_status": payload.get("readStatus"),
			"letter_type": payload.get("letterType"),
			"folder": folder,
			"raw_metadata": frappe.as_json(payload),
		}

	@staticmethod
	def _sender_name(payload: dict) -> str | None:
		# The `Letter` schema (spec:16524) has no sender-name field. `description`
		# is what actually carries it ("Invoice from <sender>"); `senderName` is
		# read in case the live API returns more than it documents, and the
		# participant id is the last resort so the column is never blank.
		return (
			payload.get("description")
			or payload.get("senderName")
			or payload.get("senderParticipantId")
			or payload.get("senderUserId")
		)

	def _record_letter_error(self, letter_id: str, exc: Exception) -> None:
		name = frappe.db.get_value(DOCTYPE, {"letter_id": letter_id}, "name")
		if name:
			frappe.db.set_value(DOCTYPE, name, "sync_error", str(exc)[:500], update_modified=False)
			frappe.db.commit()

	def _start_log(self):
		log = frappe.get_doc(
			{"doctype": "ePost Sync Log", "started_at": now_datetime(), "status": "Running"}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return log

	def _finish(self, log, status: str) -> dict:
		summary = {
			"status": status,
			"letters_seen": self.seen,
			"letters_new": self.created,
			"files_downloaded": self.downloaded,
			"letters_analyzed": self.analyzed,
			"errors": len(self.errors),
		}

		log.update(
			{
				"ended_at": now_datetime(),
				"status": status,
				"letters_seen": self.seen,
				"letters_new": self.created,
				"files_downloaded": self.downloaded,
				"letters_analyzed": self.analyzed,
				"errors": "\n".join(self.errors)[:100000] or None,
			}
		)
		log.save(ignore_permissions=True)

		frappe.db.set_single_value(
			"ePost Settings",
			{
				"last_sync_at": now_datetime(),
				"last_sync_status": (
					f"{status} — {self.seen} seen, {self.created} new, {self.downloaded} downloaded"
				),
			},
		)
		frappe.db.commit()
		return summary


def _safe_name(value: Any) -> str:
	"""Reduce a service-supplied string to characters that can only be a name.

	Frappe builds the stored file path from `file_name`, so a value shaped like
	a path would decide where the file lands.
	"""
	cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))
	return re.sub(r"^\.+", "_", cleaned) or "letter"


def _parse_datetime(value: Any) -> datetime | None:
	"""ePost sends UTC ISO-8601 (`2021-09-29T04:21:10.163Z`); Frappe stores
	naive datetimes in the site timezone."""
	if not value:
		return None

	text = str(value).strip()
	if text.endswith("Z"):
		text = text[:-1] + "+00:00"

	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		return None

	if parsed.tzinfo is None:
		return parsed

	utc_naive = parsed.astimezone(UTC).replace(tzinfo=None)
	return convert_utc_to_system_timezone(utc_naive).replace(tzinfo=None)
