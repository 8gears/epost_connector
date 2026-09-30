# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostLetter` and cannot be renamed to PascalCase.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from epost_connector.epost.exceptions import ePostError
from epost_connector.epost.sync import STATUS_RANK


class ePostLetter(Document):
	def validate(self) -> None:
		self._block_status_regression()

	def on_update(self) -> None:
		self._continue_when_supplier_is_chosen()

	def _block_status_regression(self) -> None:
		"""The pipeline runs one way, with deliberate escape hatches.

		"Ignored" is a decision a user may take at any point; a letter whose
		Purchase Invoice was deleted must be able to come back into the queue;
		and an explicit re-run (`flags.reprocess`) starts a letter over.
		"""
		previous = self.get_doc_before_save()
		if not previous or previous.status == self.status:
			return

		if STATUS_RANK.get(self.status, 0) >= STATUS_RANK.get(previous.status, 0):
			return

		if self.status == "Ignored" or self.flags.reprocess:
			return

		if previous.status == "Drafted" and not (
			self.purchase_invoice and frappe.db.exists("Purchase Invoice", self.purchase_invoice)
		):
			return

		frappe.throw(
			_("Status cannot move back from {0} to {1}").format(previous.status, self.status),
			title=_("Pipeline runs forward"),
		)

	def _continue_when_supplier_is_chosen(self) -> None:
		"""A supplier set on a waiting letter is the reviewer's answer; carry on."""
		if self.status != "Waiting for Supplier" or not self.supplier or self.flags.in_processing:
			return
		if not self.has_value_changed("supplier"):
			return
		frappe.enqueue(
			"epost_connector.inbox.process.process_letter_by_name",
			queue="long",
			enqueue_after_commit=True,
			letter_name=self.name,
			confirmed=True,
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
