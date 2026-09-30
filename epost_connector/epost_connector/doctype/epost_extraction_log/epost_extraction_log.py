# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostExtractionLog` and cannot be renamed to PascalCase.

from __future__ import annotations

from frappe.model.document import Document


class ePostExtractionLog(Document):
	"""One model call for one letter: what was asked, what came back, and what it cost. Debug data, System Manager only."""
