"""The `ePost Settings` doc and the two buttons on it.

`fetch_tenants` and `test_connection` are the only whitelisted methods that talk
to ePost outside a sync, and both are reachable from the Desk by anyone holding
the right role — so what they are allowed to do, and what they say when they
fail, is the whole of what is tested here.
"""

from __future__ import annotations

import frappe

from epost_connector.epost.client import DEFAULT_BASE_URL, ePostClient
from epost_connector.epost_connector.doctype.epost_settings.epost_settings import (
	fetch_tenants,
	test_connection,
)
from epost_connector.tests import mock_epost
from epost_connector.tests.mock_epost import COMPANY_ID, COMPANY_NAME, TENANT_ID
from epost_connector.tests.site_base import ePostSiteTestCase


class ConnectionButtonTest(ePostSiteTestCase):
	def test_test_connection_authenticates_and_makes_one_harmless_read(self):
		result = test_connection()

		self.assertTrue(result["ok"])
		self.assertEqual(result["tenant_id"], TENANT_ID)
		self.assertEqual(result["company_id"], str(COMPANY_ID))
		self.assertEqual(
			result["unread_letters"], sum(1 for x in self.state.inbox if x["readStatus"] == "UNREAD")
		)

	def test_test_connection_only_ever_reads(self):
		test_connection()

		verbs = {method for method, path, _q in self.state.calls if path.startswith("/epost/")}
		self.assertEqual(verbs, {"GET"})

	def test_a_failure_is_reported_as_a_message_rather_than_a_traceback(self):
		self.configure_settings(api_base_url=f"{self.mock.base_url}/nowhere")

		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			test_connection()

		self.assertTrue(str(caught.exception).strip())

	def test_fetch_tenants_returns_what_the_form_needs_to_choose_from(self):
		tenants = fetch_tenants()

		self.assertEqual(len(tenants), 1)
		self.assertEqual(tenants[0]["tenant_id"], TENANT_ID)
		self.assertEqual(tenants[0]["company_name"], COMPANY_NAME)

	def test_fetch_tenants_says_so_when_the_credentials_are_wrong(self):
		self.configure_settings(username="nobody@example.com")

		with self.assertRaises(frappe.exceptions.ValidationError):
			fetch_tenants()

	def test_neither_button_is_open_to_a_user_without_the_roles(self):
		"""Both reach ePost with the stored credentials, so both are gated."""
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		for method in (fetch_tenants, test_connection):
			with self.subTest(method=method.__name__), self.assertRaises(frappe.exceptions.PermissionError):
				method()


class SettingsValidationTest(ePostSiteTestCase):
	def test_a_trailing_slash_on_the_base_url_is_trimmed(self):
		"""It would otherwise become a double slash in every path."""
		self.configure_settings(api_base_url=f"{self.mock.base_url}/")

		self.assertEqual(frappe.db.get_single_value("ePost Settings", "api_base_url"), self.mock.base_url)

	def test_an_empty_base_url_falls_back_to_the_documented_host(self):
		settings = frappe.get_doc("ePost Settings")
		settings.api_base_url = ""
		settings.save(ignore_permissions=True)

		self.assertEqual(settings.api_base_url, DEFAULT_BASE_URL)

	def test_the_sync_cannot_be_enabled_without_credentials(self):
		"""Enabled with no password means an hourly job that only ever fails."""
		settings = frappe.get_doc("ePost Settings")
		settings.username = None
		settings.enabled = 1

		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			settings.save(ignore_permissions=True)

		self.assertIn("username and password", str(caught.exception))

	def test_the_client_reads_the_stored_password_back_out(self):
		"""It lives in a Password field, so it is not on the doc as plain text."""
		client = ePostClient.from_settings()

		self.assertEqual(client.password, mock_epost.PASSWORD)
		self.assertEqual(client.base_url, self.mock.base_url)
		self.assertEqual(client.tenant_id, TENANT_ID)

	def test_the_password_is_never_readable_as_an_ordinary_field(self):
		"""A Password field is kept out of the doc's own storage.

		Anything that reads settings generically — an export, a diff, a log of
		the doc — would otherwise carry the ePost password with it.
		"""
		settings = frappe.get_doc("ePost Settings")

		self.assertNotEqual(frappe.db.get_single_value("ePost Settings", "password"), mock_epost.PASSWORD)
		self.assertNotEqual(settings.password, mock_epost.PASSWORD)
		self.assertEqual(settings.get_password("password", raise_exception=False), mock_epost.PASSWORD)
