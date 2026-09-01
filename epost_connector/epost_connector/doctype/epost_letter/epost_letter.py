# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostLetter` and cannot be renamed to PascalCase.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from epost_connector.epost.exceptions import ePostError
from epost_connector.epost.sync import STATUS_RANK


class ePostLetter(Document):
	def before_insert(self) -> None:
		# `Document.__init__` fills missing Link fields from the site and user
		# defaults, so a letter nobody has read yet arrives already carrying the
		# default currency. Every field in the Extraction section has to mean
		# "this is what was read off the document": the importer goes on to
		# report a currency here as detected on the letter, and it would be
		# saying that about a PDF nothing has opened.
		self.currency = None

	def validate(self) -> None:
		self._block_status_regression()

	def _block_status_regression(self) -> None:
		"""The pipeline runs one way, with two deliberate escape hatches.

		"Ignored" is a decision a user may take at any point, and a letter whose
		Purchase Invoice was deleted must be able to come back into the queue.
		"""
		previous = self.get_doc_before_save()
		if not previous or previous.status == self.status:
			return

		if STATUS_RANK.get(self.status, 0) >= STATUS_RANK.get(previous.status, 0):
			return

		if self.status == "Ignored":
			return

		if previous.status == "Imported" and not (
			self.purchase_invoice and frappe.db.exists("Purchase Invoice", self.purchase_invoice)
		):
			return

		frappe.throw(
			_("Status cannot move back from {0} to {1}").format(previous.status, self.status),
			title=_("Pipeline runs forward"),
		)


@frappe.whitelist()
def download_pdf(letter_name: str) -> dict:
	"""Desk button: fetch this letter's PDF from ePost if it is missing."""
	from epost_connector.epost.sync import LetterSync

	letter = frappe.get_doc("ePost Letter", letter_name)
	letter.check_permission("write")

	try:
		LetterSync().download(letter)
	except ePostError as exc:
		frappe.throw(str(exc), title=_("Download failed"))

	return {"file": letter.file, "status": letter.status}
