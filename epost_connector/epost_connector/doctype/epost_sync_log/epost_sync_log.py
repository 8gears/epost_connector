# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostSyncLog` and cannot be renamed to PascalCase.

from __future__ import annotations

from frappe.model.document import Document


class ePostSyncLog(Document):
	"""One sync run. Written only by the sync engine; read-only in Desk."""
