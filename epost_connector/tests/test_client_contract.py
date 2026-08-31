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
from epost_connector.epost.client import ePostClient
from epost_connector.epost.exceptions import ePostWriteAttempt


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
		return StubResponse(404, {"message": "unexpected", "code": "X"}, url=url)


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
