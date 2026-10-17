"""Hand the pipeline's own `Not Bookable` decisions back to a person.

Until now a rule, a missing amount or a date outside the books marked a letter
Not Bookable on the strength of what the model read. Those letters carry the
pipeline's note; a letter a person marked has none. They become `Needs Review`.
Every processed letter also gets the document type its log holds, so the list
can be filtered by it.
"""

from __future__ import annotations

import frappe


def execute() -> None:
	frappe.db.sql(
		"""update `tabePost Letter` set status = 'Needs Review'
		where status = 'Not Bookable' and ifnull(processing_note, '') != ''"""
	)
	frappe.db.sql(
		"""update `tabePost Letter` letter
		join `tabePost Extraction Log` log on log.name = letter.extraction_log
		set letter.document_kind = log.document_kind
		where ifnull(letter.document_kind, '') = ''"""
	)
