# Frappe resolves a controller class by `doctype.replace(" ", "")`, so this
# class must stay `ePostSettings` and cannot be renamed to PascalCase.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from epost_connector.epost.client import DEFAULT_BASE_URL, ePostClient
from epost_connector.epost.exceptions import ePostError
from epost_connector.extraction.registry import EXTRACTORS


class ePostSettings(Document):
	def validate(self) -> None:
		self.api_base_url = (self.api_base_url or DEFAULT_BASE_URL).rstrip("/")

		if self.extractor and self.extractor not in EXTRACTORS:
			frappe.throw(
				_("Unknown extractor {0}. Registered: {1}").format(
					self.extractor, ", ".join(k for k in EXTRACTORS if k)
				)
			)

		if self.enabled and not (self.username and self.get_password("password", raise_exception=False)):
			frappe.throw(_("Set a username and password before enabling the ePost sync"))


@frappe.whitelist()
def fetch_tenants() -> list[dict]:
	"""Tenants the stored credentials can reach, for the settings form to choose from."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	try:
		return ePostClient.from_settings().list_tenants()
	except ePostError as exc:
		frappe.throw(str(exc), title=_("Could not reach ePost"))


@frappe.whitelist()
def test_connection() -> dict:
	"""Authenticate and make one harmless read, to prove the credentials work."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	try:
		client = ePostClient.from_settings()
		client.authenticate()
		return {
			"ok": True,
			"tenant_id": client.tenant_id,
			"company_id": client.company_id,
			"unread_letters": client.get_unread_count(),
		}
	except ePostError as exc:
		frappe.throw(str(exc), title=_("Connection failed"))
