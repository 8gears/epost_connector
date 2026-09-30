"""Map the old pipeline statuses onto the new ones.

`Analyzed` letters have an extraction but were never taken further, so they go
back to `Downloaded` and the next processing run continues them from their log
without calling the extractor again. `Imported` letters have a draft: `Drafted`.
"""

from __future__ import annotations

import frappe


def execute() -> None:
	frappe.db.sql("update `tabePost Letter` set status = 'Downloaded' where status = 'Analyzed'")
	frappe.db.sql("update `tabePost Letter` set status = 'Drafted' where status = 'Imported'")
