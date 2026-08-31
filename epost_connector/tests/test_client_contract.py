"""The read-only contract and the pagination shape, locked down.

Runs without a Frappe site: `ePostClient` only imports frappe inside
`from_settings`, so a stub session is enough. Integration tests against a mock
ePost server live elsewhere.

	python -m unittest epost_connector.tests.test_client_contract
"""

from __future__ import annotations

import ast
import inspect
import json
import unittest

from epost_connector.epost import client as client_module
from epost_connector.epost.client import INBOX_FOLDER, STORAGE_ROOT, ePostClient
from epost_connector.epost.exceptions import (
	ePostContentError,
	ePostPaginationLimit,
	ePostWriteAttempt,
)


class StubResponse:
	def __init__(self, status_code: int, payload=None, content: bytes = b"", url: str = ""):
		self.status_code = status_code
		self.content = content
		self.url = url
		self.reason = "stub"
		self._payload = payload
		self.text = json.dumps(payload) if payload is not None else ""

	@property
	def ok(self) -> bool:
		return self.status_code < 400

	def json(self):
		if self._payload is None:
			raise ValueError("not json")
		return self._payload


class StubSession:
	"""Answers the auth endpoints and serves `total` letters from one array."""

	def __init__(self, total: int = 250):
		self.calls: list[tuple] = []
		self.letters = [{"id": str(i), "letterTitle": f"Letter {i}"} for i in range(total)]

	def request(self, method, url, headers=None, timeout=None, **kwargs):
		self.calls.append((method, url, kwargs.get("params"), kwargs.get("data")))

		if url.endswith("/core/latest/tenants"):
			return StubResponse(200, [{"tenant_id": "t1", "company_id": 42, "company_name": "8gears"}])
		if url.endswith("/core/latest/token"):
			return StubResponse(
				200,
				{"access_token": "tok", "expires_in": 300, "refreshToken": "ref", "refresh_expires_in": 1800},
			)
		if url.endswith("/epost/v2/letters"):
			params = kwargs["params"]
			start, limit = params["offset"], params["limit"]
			return StubResponse(200, self.letters[start : start + limit], url=url)
		if url.endswith("/epost/v2/archives/directories"):
			return StubResponse(200, [], url=url)
		if url.endswith("/epost/v2/archives/letters"):
			return StubResponse(200, [], url=url)
		return StubResponse(404, {"message": "unexpected", "code": "X"}, url=url)


class WindowSession(StubSession):
	"""A service that ignores `offset` and always answers with the same window."""

	def request(self, method, url, headers=None, timeout=None, **kwargs):
		if url.endswith("/epost/v2/letters"):
			self.calls.append((method, url, kwargs.get("params"), None))
			return StubResponse(200, self.letters[: kwargs["params"]["limit"]], url=url)
		return super().request(method, url, headers=headers, timeout=timeout, **kwargs)


class ReadOnlyContractTest(unittest.TestCase):
	"""If any of these fail, the app can mutate the letterbox n8n owns."""

	def setUp(self):
		self.session = StubSession()
		self.client = ePostClient("user@example.com", "pw", session=self.session, page_size=100)

	def test_non_get_below_epost_is_refused(self):
		for verb, path in (
			("POST", "/epost/v2/letters/1/read"),
			("POST", "/epost/v2/letters/1/accept"),
			("POST", "/epost/v2/letters/1/reject"),
			("POST", "/epost/v2/letters/1/archive"),
			("POST", "/epost/v2/letters/1/restore"),
			("DELETE", "/epost/v2/letters/1"),
		):
			with self.subTest(verb=verb, path=path), self.assertRaises(ePostWriteAttempt):
				self.client._send(verb, path)

	def test_module_issues_no_non_get_call_to_epost(self):
		"""Static check: only the two /core/latest auth endpoints use POST."""
		tree = ast.parse(inspect.getsource(client_module))
		non_get = []

		for node in ast.walk(tree):
			if not (
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and node.func.attr in ("_send", "_request")
				and node.args
				and isinstance(node.args[0], ast.Constant)
			):
				continue
			if node.args[0].value == "GET":
				continue

			target = node.args[1] if len(node.args) > 1 else None
			non_get.append(getattr(target, "id", None) or getattr(target, "value", "<expr>"))

		self.assertEqual(sorted(non_get), ["TENANTS_PATH", "TOKEN_PATH"])

	def test_no_state_changing_methods_exist(self):
		forbidden = {
			"accept_letter",
			"archive_letter",
			"delete_letter",
			"mark_read",
			"reject_letter",
			"restore_letter",
			"set_read_status",
		}
		self.assertEqual(forbidden & set(dir(ePostClient)), set())


class PaginationTest(unittest.TestCase):
	def setUp(self):
		self.session = StubSession(total=250)
		self.client = ePostClient("user@example.com", "pw", session=self.session, page_size=100)

	def _list_calls(self):
		return [c for c in self.session.calls if c[1].endswith("/epost/v2/letters")]

	def test_paginates_to_exhaustion_and_stops_on_a_short_page(self):
		letters = list(self.client.iter_letters())

		self.assertEqual(len(letters), 250)
		self.assertEqual(len({letter["id"] for letter in letters}), 250)
		self.assertEqual([c[2]["offset"] for c in self._list_calls()], [0, 100, 200])

	def test_required_and_defaulted_query_params(self):
		self.client.list_letters()
		params = self._list_calls()[0][2]

		# spec:11622 — letter-types is required; spec:11614/11660 — folder and
		# read-status defaults.
		self.assertEqual(params["letter-types"], ["CLASSIC_LETTER"])
		self.assertEqual(params["letter-folder"], "INBOX_FOLDER")
		self.assertEqual(params["read-status"], "ALL")

	def test_limit_is_capped_at_the_documented_maximum(self):
		self.client.list_letters(limit=99999)
		self.assertEqual(self._list_calls()[0][2]["limit"], ePostClient.MAX_PAGE_SIZE)


class WindowFallbackTest(unittest.TestCase):
	"""The spec documents `offset`; a reference client says it is a window."""

	def test_an_ignored_offset_is_reported_not_looped(self):
		session = WindowSession(total=250)
		client = ePostClient("user@example.com", "pw", session=session, page_size=100)

		with self.assertRaises(ePostPaginationLimit) as caught:
			list(client.iter_letters())

		# The first window is still delivered before the limit is reported.
		self.assertEqual(caught.exception.reachable, 100)
		self.assertIn("ignoring `offset`", str(caught.exception))

	def test_the_first_window_is_yielded_before_the_limit_is_raised(self):
		session = WindowSession(total=250)
		client = ePostClient("user@example.com", "pw", session=session, page_size=100)

		delivered = []
		with self.assertRaises(ePostPaginationLimit):
			for letter in client.iter_letters():
				delivered.append(letter["id"])

		self.assertEqual(len(delivered), 100)
		self.assertEqual(len(set(delivered)), 100)


class ContentValidationTest(unittest.TestCase):
	"""A 200 is not proof of a PDF: gateway error pages and 0 bytes were seen."""

	def _client_returning(self, body: bytes):
		class ContentSession(StubSession):
			def request(self, method, url, headers=None, timeout=None, **kwargs):
				if url.endswith("/content"):
					return StubResponse(200, content=body, url=url)
				return super().request(method, url, headers=headers, timeout=timeout, **kwargs)

		return ePostClient("user@example.com", "pw", session=ContentSession())

	def test_an_html_error_page_is_refused(self):
		client = self._client_returning(b"<html><body>gateway error</body></html>")
		with self.assertRaises(ePostContentError):
			client.get_letter_content("1")

	def test_an_empty_body_is_refused(self):
		client = self._client_returning(b"")
		with self.assertRaises(ePostContentError):
			client.get_letter_content("1")

	def test_a_pdf_behind_a_byte_order_mark_is_accepted(self):
		client = self._client_returning(b"\xef\xbb\xbf%PDF-1.4 body")
		self.assertTrue(client.get_letter_content("1").endswith(b"body"))


class RedactionTest(unittest.TestCase):
	"""The gateway can quote our own credentials back at us."""

	def test_an_echoed_password_never_reaches_the_error_message(self):
		password = "correct-horse-battery-staple"

		class EchoSession(StubSession):
			def request(self, method, url, headers=None, timeout=None, **kwargs):
				if url.endswith("/epost/v2/letters"):
					return StubResponse(400, {"message": f"rejected form: password={password}"}, url=url)
				return super().request(method, url, headers=headers, timeout=timeout, **kwargs)

		client = ePostClient("user@example.com", password, session=EchoSession())
		with self.assertRaises(Exception) as caught:
			client.list_letters()

		self.assertNotIn(password, str(caught.exception))
		self.assertIn("[redacted]", str(caught.exception))


class ArchiveCoverageTest(unittest.TestCase):
	"""A letter archived on ePost must not vanish from ERPNext."""

	def test_inbox_and_archive_are_both_listed_and_deduplicated(self):
		class ArchiveSession(StubSession):
			def request(self, method, url, headers=None, timeout=None, **kwargs):
				if url.endswith("/epost/v2/letters"):
					return StubResponse(200, [{"id": "1"}, {"id": "2"}], url=url)
				if url.endswith("/epost/v2/archives/directories"):
					return StubResponse(
						200,
						[
							{"directoryId": "", "directoryName": "ePost Scancenter"},
							{"directoryId": "d1", "directoryName": "Rechnungen"},
						],
						url=url,
					)
				if url.endswith("/epost/v2/archives/letters"):
					if kwargs["params"].get("directory-id") == "d1":
						return StubResponse(200, [{"id": "4"}, {"id": "2"}], url=url)
					return StubResponse(200, [{"id": "3"}], url=url)
				return super().request(method, url, headers=headers, timeout=timeout, **kwargs)

		client = ePostClient("user@example.com", "pw", session=ArchiveSession())
		found = {letter["id"]: folder for letter, folder in client.iter_all_letters()}

		self.assertEqual(found, {"1": INBOX_FOLDER, "2": INBOX_FOLDER, "3": STORAGE_ROOT, "4": "Rechnungen"})

	def test_a_decomposed_folder_name_is_normalised(self):
		class NfdSession(StubSession):
			def request(self, method, url, headers=None, timeout=None, **kwargs):
				if url.endswith("/epost/v2/letters"):
					return StubResponse(200, [], url=url)
				if url.endswith("/epost/v2/archives/directories"):
					# "Bürö" written decomposed: u + combining diaeresis.
					return StubResponse(200, [{"directoryId": "d1", "directoryName": "Bürö"}], url=url)
				if url.endswith("/epost/v2/archives/letters"):
					if kwargs["params"].get("directory-id") == "d1":
						return StubResponse(200, [{"id": "9"}], url=url)
					return StubResponse(200, [], url=url)
				return super().request(method, url, headers=headers, timeout=timeout, **kwargs)

		client = ePostClient("user@example.com", "pw", session=NfdSession())
		folders = [folder for _letter, folder in client.iter_all_letters()]

		self.assertEqual(folders, ["Bürö"])


class AuthTest(unittest.TestCase):
	def setUp(self):
		self.session = StubSession()
		self.client = ePostClient("user@example.com", "pw", session=self.session)

	def test_resolves_a_single_tenant_then_takes_a_password_grant(self):
		self.client.authenticate()
		auth_calls = [c for c in self.session.calls if "/core/latest/" in c[1]]

		self.assertEqual(len(auth_calls), 2)
		self.assertTrue(auth_calls[0][1].endswith("/core/latest/tenants"))
		self.assertEqual(auth_calls[1][3]["grant_type"], "password")
		self.assertEqual(auth_calls[1][3]["tenant_id"], "t1")
		self.assertEqual(self.client.company_id, "42")

	def test_accepts_either_refresh_token_spelling(self):
		# spec:17747 says `refreshToken`, spec:12775 says `refresh_token`.
		self.client.authenticate()
		self.assertEqual(self.client.token.refresh_token, "ref")


if __name__ == "__main__":
	unittest.main()
