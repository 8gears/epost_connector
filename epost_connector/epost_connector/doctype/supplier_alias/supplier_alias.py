# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `SupplierAlias` and cannot be renamed to PascalCase.

from __future__ import annotations

from frappe.model.document import Document


class SupplierAlias(Document):
	"""Another name, tax id or IBAN under which a Supplier appears on documents. Learned on every confirmed match."""
