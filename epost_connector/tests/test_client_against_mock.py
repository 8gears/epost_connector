"""`ePostClient` against a real HTTP server speaking the ePost shapes.

`test_client_contract.py` locks the contract with stub objects; this file runs
the same client over a socket, so header handling, form encoding, repeated query
parameters and status handling are exercised rather than assumed. No Frappe site
is needed: the client only imports frappe inside `from_settings`.
"""

from __future__ import annotations

import pathlib
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import requests

from epost_connector.epost.client import INBOX_FOLDER, STORAGE_ROOT, ePostClient
from epost_connector.epost.exceptions import (
	ePostAPIError,
	ePostAuthError,
	ePostContentError,
	ePostPaginationLimit,
	ePostWriteAttempt,
)
from epost_connector.tests import mock_epost
from epost_connector.tests.mock_epost import (
	ACCESS_TOKEN,
	COMPANY_ID,
	EMPTY_CONTENT_LETTER,
	HTML_CONTENT_LETTER,
	NFC_DIRECTORY_NAME,
	REFRESHED_ACCESS_TOKEN,
	TENANT_ID,
	MockePost,
	MockState,
	letter,
)


class MockServerTestCase(unittest.TestCase):
	"""Starts a mock ePost on the loopback for the duration of one test."""

	def setUp(self) -> None:
		self.mock = MockePost().start()
		self.addCleanup(self.mock.stop)
		self.state = self.mock.state

	def client(self, **kwargs) -> ePostClient:
		client = ePostClient(
			mock_epost.USERNAME,
			mock_epost.PASSWORD,
			base_url=self.mock.base_url,
			**kwargs,
		)
		# Real backoff would put seconds of sleep into the retry tests for no
		# extra coverage; the retry *count* is what matters.
		client.BACKOFF_SECONDS = 0
		# The session holds a keep-alive connection open; leaving it for the
		# garbage collector makes teardown wait on it.
		self.addCleanup(client.session.close)
		return client


class AuthFlowTest(MockServerTestCase):
	def test_credentials_resolve_a_tenant_and_then_take_a_password_grant(self):
		client = self.client()
		token = client.authenticate()

		self.assertEqual(client.tenant_id, TENANT_ID)
		# The tenants endpoint answers with an int; the token endpoint compares
		# it as a form value, so the client has to carry it as a string.
		self.assertEqual(client.company_id, str(COMPANY_ID))
		self.assertEqual(token.access_token, ACCESS_TOKEN)
		self.assertEqual(self.state.issued_tokens(), ["password"])

	def test_wrong_credentials_fail_as_an_auth_error_not_a_generic_one(self):
		client = ePostClient("nobody@example.com", "wrong-password-entirely", base_url=self.mock.base_url)
		with self.assertRaises(ePostAuthError):
			client.authenticate()

	def test_more_than_one_tenant_refuses_to_guess(self):
		# Picking one would silently read the wrong company's letterbox.
		self.state.tenants.append({"tenant_id": "t-2", "company_id": 7, "company_name": "Other"})
		client = self.client()

		with self.assertRaises(ePostAuthError) as caught:
			client.authenticate()

		self.assertIn("tenant_id", str(caught.exception))
		self.assertEqual(self.state.issued_tokens(), [])

	def test_a_configured_tenant_pair_skips_the_tenants_call(self):
		client = self.client(tenant_id=TENANT_ID, company_id=str(COMPANY_ID))
		client.authenticate()

		self.assertEqual(self.state.calls_to("/core/latest/tenants"), [])
		self.assertEqual(self.state.issued_tokens(), ["password"])

	def test_no_bearer_header_is_sent_on_the_two_auth_endpoints(self):
		self.client().authenticate()
		# Both are POSTs, and both must work before any token exists.
		self.assertEqual(len(self.state.calls_to("/core/latest/token")), 1)
		self.assertEqual(len(self.state.calls_to("/core/latest/tenants")), 1)


class TokenLifecycleTest(MockServerTestCase):
	def test_an_expired_token_is_refreshed_rather_than_re_authenticated(self):
		# Expired the moment it is handed over, so the *second* call has to
		# renew it. The first still goes out on the token it just obtained.
		self.state.expires_in = 0
		client = self.client()

		client.list_letters()
		self.assertEqual(self.state.issued_tokens(), ["password"])

		client.list_letters()

		self.assertEqual(self.state.issued_tokens(), ["password", "refresh_token"])
		self.assertEqual(client.token.access_token, REFRESHED_ACCESS_TOKEN)
		# The renewal costs one token call, not a second full tenant resolution.
		self.assertEqual(len(self.state.calls_to("/core/latest/tenants")), 1)

	def test_a_rejected_refresh_falls_back_to_the_password_grant(self):
		self.state.expires_in = 0
		self.state.accept_refresh_grant = False
		client = self.client()

		client.list_letters()
		letters = client.list_letters()

		self.assertEqual(self.state.issued_tokens(), ["password", "refresh_token", "password"])
		self.assertEqual(client.token.access_token, ACCESS_TOKEN)
		self.assertTrue(letters)

	def test_a_zero_second_lifetime_means_expired_not_defaulted(self):
		"""`expires_in: 0` is the service saying the token is already dead.

		Read as "absent" it would earn the five-minute default instead, and the
		client would keep presenting a token it was told not to until the 401s
		started coming back.
		"""
		self.state.expires_in = 0
		client = self.client()
		client.authenticate()

		self.assertTrue(client.token.is_expired())

	def test_a_401_re_authenticates_once_and_the_call_then_succeeds(self):
		"""A token revoked server-side before it expired looks fine to the client."""
		self.state.reject_bearer_times = 1
		client = self.client()

		letters = client.list_letters()

		self.assertEqual(len(letters), len(self.state.inbox))
		self.assertEqual(self.state.issued_tokens(), ["password", "password"])

	def test_a_401_that_survives_re_authentication_is_raised_not_looped(self):
		self.state.reject_bearer_times = 99
		client = self.client()

		with self.assertRaises(ePostAuthError):
			client.list_letters()
		# Exactly one retry: the first call, then one more after re-authenticating.
		self.assertEqual(len(self.state.issued_tokens()), 2)


class QueryContractTest(MockServerTestCase):
	def test_letter_types_reaches_the_wire_as_a_repeated_parameter(self):
		client = self.client()
		client.list_letters(letter_types=("CLASSIC_LETTER", "SMART_LETTER"))

		_method, _path, query = self.state.calls_to("/epost/v2/letters")[0]
		self.assertEqual(query["letter-types"], ["CLASSIC_LETTER", "SMART_LETTER"])
		self.assertEqual(query["letter-folder"], ["INBOX_FOLDER"])

	def test_the_server_rejects_a_listing_without_letter_types(self):
		"""spec:11622 — required. Proves the 400 the client must never provoke."""
		client = self.client()
		client.authenticate()

		response = requests.get(
			f"{self.mock.base_url}/epost/v2/letters",
			headers={"Authorization": f"Bearer {client.token.access_token}"},
			timeout=5,
		)
		self.assertEqual(response.status_code, 400)

	def test_date_bounds_are_only_sent_as_a_pair(self):
		client = self.client()
		client.list_letters(from_date="2024-01-01")
		client.list_letters(from_date="2024-01-01", to_date="2024-12-31")

		one, both = self.state.calls_to("/epost/v2/letters")
		self.assertNotIn("from-date", one[2])
		self.assertEqual(both[2]["from-date"], ["2024-01-01"])
		self.assertEqual(both[2]["to-date"], ["2024-12-31"])

	def test_the_unread_count_is_read_from_a_bare_integer_body(self):
		client = self.client()
		unread = sum(1 for x in self.state.inbox if x["readStatus"] == "UNREAD")
		self.assertEqual(client.get_unread_count(), unread)

	def test_the_unread_count_is_also_read_from_a_count_object(self):
		"""spec:11815 says a bare integer; a reference client saw {"count": n}.

		Neither is confirmable from here, so both have to work — and the failure
		if only one did would be a TypeError on a number, not a clear message.
		"""
		self.state.count_as_object = True
		client = self.client()
		unread = sum(1 for x in self.state.inbox if x["readStatus"] == "UNREAD")

		self.assertEqual(client.get_unread_count(), unread)

	def test_a_listing_inside_an_envelope_is_not_read_as_an_empty_letterbox(self):
		"""The spec says a bare array. A service that grew a wrapper must not
		make every letter disappear — that reads as "the letterbox is empty",
		which is the one wrong answer a sync acts on destructively."""
		for key in ("letters", "content", "items", "data"):
			with self.subTest(envelope=key):
				self.state.list_envelope = key
				client = self.client()

				self.assertEqual(len(client.list_letters()), len(self.state.inbox))

	def test_an_unrecognised_envelope_is_an_error_rather_than_an_empty_list(self):
		self.state.list_envelope = "payload"
		client = self.client()

		with self.assertRaises(ePostAPIError) as caught:
			client.list_letters()
		self.assertIn("Expected a JSON array", str(caught.exception))

	def test_the_default_page_size_asks_for_the_documented_maximum(self):
		"""48 is the API's default; asking for it would page 21x more often."""
		self.assertEqual(ePostClient.DEFAULT_PAGE_SIZE, 1000)
		self.assertEqual(ePostClient.MAX_PAGE_SIZE, 1000)

		self.client().list_letters()
		_m, _p, query = self.state.calls_to("/epost/v2/letters")[0]
		self.assertEqual(query["limit"], ["1000"])


class SearchTest(MockServerTestCase):
	def test_a_search_finds_letters_by_a_word_in_them(self):
		client = self.client()

		hits = client.search_letters("Kontoauszug")

		self.assertEqual([x["id"] for x in hits], ["inbox-2"])

	def test_the_search_term_is_sent_under_both_names_the_sources_disagree_on(self):
		"""spec:11974 calls it `value`; a working reference client sends
		`keyword`. Sending both is the only option that cannot be wrong."""
		client = self.client()
		client.search_letters("Kontoauszug")

		_m, _p, query = self.state.calls_to("/epost/v2/letters/search")[0]
		self.assertEqual(query["value"], ["Kontoauszug"])
		self.assertEqual(query["keyword"], ["Kontoauszug"])

	def test_the_search_is_a_get(self):
		client = self.client()
		client.search_letters("anything")

		self.assertEqual({m for m, _p, _q in self.state.calls_to("/epost/v2/letters/search")}, {"GET"})

	def test_a_single_letter_comes_back_as_an_object(self):
		client = self.client()
		self.assertEqual(client.get_letter("inbox-1")["id"], "inbox-1")

	def test_a_missing_letter_is_an_api_error_carrying_the_status(self):
		client = self.client()
		with self.assertRaises(ePostAPIError) as caught:
			client.get_letter("does-not-exist")
		self.assertEqual(caught.exception.status_code, 404)


class PaginationTest(MockServerTestCase):
	def test_every_letter_is_returned_across_pages(self):
		self.state.inbox = [letter(f"p-{i}") for i in range(25)]
		client = self.client(page_size=10)

		ids = [x["id"] for x in client.iter_letters()]

		self.assertEqual(len(ids), 25)
		self.assertEqual(len(set(ids)), 25)
		offsets = [q["offset"][0] for _m, _p, q in self.state.calls_to("/epost/v2/letters")]
		self.assertEqual(offsets, ["0", "10", "20"])

	def test_a_page_exactly_the_size_of_the_limit_is_not_read_as_the_end(self):
		"""The dangerous case: a full page is ambiguous until the next one comes back."""
		self.state.inbox = [letter(f"p-{i}") for i in range(10)]
		client = self.client(page_size=10)

		ids = [x["id"] for x in client.iter_letters()]

		self.assertEqual(len(ids), 10)
		# Two calls: the full page, then the empty one that proves it was the end.
		self.assertEqual(len(self.state.calls_to("/epost/v2/letters")), 2)

	def test_a_service_that_ignores_offset_is_reported_not_looped(self):
		self.state.inbox = [letter(f"p-{i}") for i in range(25)]
		self.state.ignore_offset = True
		client = self.client(page_size=10)

		delivered = []
		with self.assertRaises(ePostPaginationLimit) as caught:
			for item in client.iter_letters():
				delivered.append(item["id"])

		self.assertEqual(len(delivered), 10)
		self.assertEqual(caught.exception.reachable, 10)

	def test_the_limit_never_exceeds_the_documented_maximum(self):
		client = self.client(page_size=99999)
		client.list_letters()

		_m, _p, query = self.state.calls_to("/epost/v2/letters")[0]
		self.assertEqual(query["limit"], [str(ePostClient.MAX_PAGE_SIZE)])


class ArchiveCoverageTest(MockServerTestCase):
	def test_inbox_root_storage_and_folders_are_all_covered(self):
		client = self.client()
		found = {payload["id"]: folder for payload, folder in client.iter_all_letters()}

		for payload in self.state.inbox:
			self.assertEqual(found[payload["id"]], INBOX_FOLDER)
		# spec:11370 — a directory-less archive listing is root storage only,
		# so a filed letter is reachable only through its folder.
		self.assertEqual(found["arch-2"], STORAGE_ROOT)
		self.assertEqual(found["arch-1"], "Rechnungen")

	def test_a_decomposed_folder_name_is_normalised_to_one_string(self):
		client = self.client()
		found = {payload["id"]: folder for payload, folder in client.iter_all_letters()}

		folder = found["arch-3"]
		self.assertEqual(folder, NFC_DIRECTORY_NAME)
		# The bytes differ even though the two strings look identical on screen;
		# without normalisation ERPNext would hold two folders called "Bürö".
		self.assertNotEqual(folder, mock_epost.NFD_DIRECTORY_NAME)

	def test_the_branded_directory_without_an_id_is_never_listed_by_id(self):
		"""spec:15291 — its id is empty, and its documents are in root storage."""
		client = self.client()
		list(client.iter_all_letters())

		by_id = [
			q["directory-id"][0]
			for _m, _p, q in self.state.calls_to("/epost/v2/archives/letters")
			if q.get("directory-id")
		]
		self.assertEqual(sorted(set(by_id)), ["dir-one", "dir-two"])

	def test_a_letter_in_both_the_inbox_and_the_archive_is_yielded_once(self):
		self.state.archive.append(letter("inbox-1"))
		client = self.client()

		ids = [payload["id"] for payload, _folder in client.iter_all_letters()]
		self.assertEqual(ids.count("inbox-1"), 1)


class ContentValidationTest(MockServerTestCase):
	def test_a_pdf_comes_back_whole(self):
		client = self.client()
		content = client.get_letter_content("inbox-1")

		self.assertTrue(content.startswith(b"%PDF-"))
		self.assertTrue(content.rstrip().endswith(b"%%EOF"))
		# The id is in the bytes, so fetching the wrong letter is detectable.
		self.assertIn(b"inbox-1", content)

	def test_an_html_error_page_served_with_200_is_refused(self):
		client = self.client()

		with self.assertRaises(ePostContentError) as caught:
			client.get_letter_content(HTML_CONTENT_LETTER)
		self.assertIn("not a PDF", str(caught.exception))

	def test_an_empty_body_served_with_200_is_refused(self):
		client = self.client()

		with self.assertRaises(ePostContentError) as caught:
			client.get_letter_content(EMPTY_CONTENT_LETTER)
		self.assertIn("no bytes", str(caught.exception))

	def test_both_bad_answers_arrive_as_a_200(self):
		"""Otherwise the status code alone would have caught them."""
		client = self.client()
		client.authenticate()

		for letter_id in (HTML_CONTENT_LETTER, EMPTY_CONTENT_LETTER):
			with self.subTest(letter=letter_id):
				response = requests.get(
					f"{self.mock.base_url}/epost/v2/letters/{letter_id}/content",
					headers={"Authorization": f"Bearer {client.token.access_token}"},
					timeout=5,
				)
				self.assertEqual(response.status_code, 200)

	def test_a_thumbnail_is_returned_as_bytes(self):
		client = self.client()
		self.assertTrue(client.get_letter_thumbnail("inbox-1").startswith(b"\xff\xd8\xff"))


class RetryTest(MockServerTestCase):
	def test_a_retryable_status_is_retried_and_then_succeeds(self):
		self.state.force_status = [{"status": 503, "path": "/epost/v2/letters"}]
		client = self.client()

		letters = client.list_letters()

		self.assertEqual(len(letters), len(self.state.inbox))
		self.assertEqual(len(self.state.calls_to("/epost/v2/letters")), 2)

	def test_a_persistent_retryable_status_gives_up_after_the_attempt_budget(self):
		self.state.force_status = [{"status": 503, "path": "/epost/v2/letters"} for _ in range(10)]
		client = self.client()

		with self.assertRaises(ePostAPIError) as caught:
			client.list_letters()

		self.assertEqual(caught.exception.status_code, 503)
		self.assertEqual(len(self.state.calls_to("/epost/v2/letters")), ePostClient.MAX_ATTEMPTS)

	def test_a_400_is_not_retried(self):
		self.state.force_status = [{"status": 400, "path": "/epost/v2/letters"}]
		client = self.client()

		with self.assertRaises(ePostAPIError):
			client.list_letters()
		self.assertEqual(len(self.state.calls_to("/epost/v2/letters")), 1)


class RedactionTest(MockServerTestCase):
	def test_credentials_the_gateway_quotes_back_never_reach_the_message(self):
		client = self.client()

		with self.assertRaises(ePostAPIError) as caught:
			client.get_letter(mock_epost.ECHO_SECRET_LETTER)

		message = str(caught.exception)
		self.assertNotIn(mock_epost.PASSWORD, message)
		self.assertNotIn(ACCESS_TOKEN, message)
		self.assertIn("[redacted]", message)

	def test_the_bearer_token_is_redacted_and_not_only_the_password(self):
		client = self.client()
		client.authenticate()

		with self.assertRaises(ePostAPIError) as caught:
			client.get_letter(mock_epost.ECHO_SECRET_LETTER)

		# The message quotes both; neither may survive into an ePost Sync Log row.
		self.assertNotIn(client.token.access_token, str(caught.exception))

	def test_a_short_password_is_left_alone_rather_than_mangling_the_message(self):
		"""Blanking every occurrence of a four-character secret would replace
		ordinary runs of letters elsewhere in the text and protect nothing: the
		value is too short to be worth hiding and too short to match uniquely."""
		client = self._client_with_password("dog")

		self.assertEqual(client._redact("the dogged gateway rejected it"), "the dogged gateway rejected it")

	def test_the_length_at_which_redaction_starts_is_eight_characters(self):
		for secret, expected in (("1234567", False), ("12345678", True)):
			with self.subTest(secret=secret):
				client = self._client_with_password(secret)

				self.assertEqual("[redacted]" in client._redact(f"sent password={secret}"), expected)

	def _client_with_password(self, password: str) -> ePostClient:
		client = ePostClient("nobody@example.com", password, base_url=self.mock.base_url)
		self.addCleanup(client.session.close)
		return client


class ReadOnlySurfaceTest(MockServerTestCase):
	"""If any of these fail, the app can mutate the letterbox n8n owns."""

	APP_ROOT = pathlib.Path(__file__).resolve().parent.parent
	TESTS_DIR = pathlib.Path(__file__).resolve().parent

	def test_the_client_refuses_a_write_before_it_reaches_the_network(self):
		client = self.client()
		for verb, path in (
			("POST", "/epost/v2/letters/inbox-1/read"),
			("POST", "/epost/v2/letters/inbox-1/archive"),
			("PATCH", "/epost/v2/letters/inbox-1/archive"),
			("DELETE", "/epost/v2/letters/inbox-1"),
		):
			with self.subTest(verb=verb), self.assertRaises(ePostWriteAttempt):
				client._send(verb, path)

		self.assertEqual([c for c in self.state.calls if c[1].startswith("/epost/")], [])

	def test_the_server_would_refuse_one_too(self):
		"""So a passing suite is not an artefact of the mock being permissive."""
		client = self.client()
		client.authenticate()

		response = requests.post(
			f"{self.mock.base_url}/epost/v2/letters/inbox-1/read",
			headers={"Authorization": f"Bearer {client.token.access_token}"},
			timeout=5,
		)
		self.assertEqual(response.status_code, 405)

	def test_no_module_in_the_app_names_a_state_changing_endpoint(self):
		"""A grep, deliberately: a write could be added anywhere, not just the client."""
		forbidden = re.compile(r"/epost/v\d+/letters/[^\"']*/(read|accept|reject|archive|restore)")
		offenders = []

		for path in self.APP_ROOT.rglob("*.py"):
			# The tests themselves name these endpoints in order to prove they
			# are refused; only shipped code is under scrutiny here.
			if path.is_relative_to(self.TESTS_DIR):
				continue
			if forbidden.search(path.read_text()):
				offenders.append(str(path.relative_to(self.APP_ROOT)))

		self.assertEqual(offenders, [])
		# The scan is worthless if it matched nothing anywhere, so prove the
		# pattern does fire on the file that deliberately contains such a path.
		self.assertTrue(forbidden.search((self.TESTS_DIR / "test_client_contract.py").read_text()))

	def test_the_client_exposes_no_write_method(self):
		forbidden = {
			"accept_letter",
			"archive_letter",
			"delete_letter",
			"mark_read",
			"move_letter",
			"reject_letter",
			"restore_letter",
			"set_read_status",
			"upload_letter",
		}
		self.assertEqual(forbidden & set(dir(ePostClient)), set())

		# And nothing that merely *looks* read-only either: every public method
		# on the client must be one of the known readers, so a write added later
		# under a name nobody thought to forbid still fails this test.
		known_readers = {
			"authenticate",
			"from_settings",
			"get_letter",
			"get_letter_content",
			"get_letter_thumbnail",
			"get_unread_count",
			"iter_all_letters",
			"iter_letters",
			"list_archive_letters",
			"list_directories",
			"list_letters",
			"list_tenants",
			"search_letters",
		}
		public = {
			name
			for name, value in vars(ePostClient).items()
			if not name.startswith("_") and callable(getattr(value, "__func__", value))
		}
		self.assertEqual(public, known_readers)


class _Recorder(BaseHTTPRequestHandler):
	"""Stands in for the letterbox and records the verb and body that arrived."""

	protocol_version = "HTTP/1.1"
	#: Shared across handler threads on purpose — one request per test, and the
	#: assertion is about what arrived here at all.
	received: ClassVar[list[tuple[str, str, str]]] = []

	def log_message(self, *args) -> None:
		pass

	def _record(self, method: str) -> None:
		length = int(self.headers.get("Content-Length") or 0)
		type(self).received.append((method, self.path, self.rfile.read(length).decode() if length else ""))
		self.send_response(200)
		self.send_header("Content-Length", "0")
		self.end_headers()

	def do_GET(self) -> None:
		self._record("GET")

	def do_POST(self) -> None:
		self._record("POST")

	def do_PUT(self) -> None:
		self._record("PUT")


class RedirectFollowingTest(unittest.TestCase):
	"""A redirect must not become the write the guard exists to prevent.

	`requests` follows redirects on its own and never consults
	`_guard_read_only`, and 307/308 keep both the verb and the body. Before this
	was closed, a token POST redirected below `/epost/` arrived at the letterbox
	as a POST with the ePost password in its body.
	"""

	def setUp(self) -> None:
		_Recorder.received = []

		self.letterbox = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
		self.letterbox.daemon_threads = True
		threading.Thread(target=self.letterbox.serve_forever, daemon=True).start()
		self.addCleanup(self.letterbox.server_close)
		self.addCleanup(self.letterbox.shutdown)

		target = f"http://127.0.0.1:{self.letterbox.server_address[1]}/epost/v2/letters/inbox-1/read"

		class _Redirector(BaseHTTPRequestHandler):
			protocol_version = "HTTP/1.1"

			def log_message(self, *args) -> None:
				pass

			def do_POST(self) -> None:
				# 308 keeps the method and the body; 307 behaves the same.
				self.send_response(308)
				self.send_header("Location", target)
				self.send_header("Content-Length", "0")
				self.end_headers()

		self.api = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
		self.api.daemon_threads = True
		threading.Thread(target=self.api.serve_forever, daemon=True).start()
		self.addCleanup(self.api.server_close)
		self.addCleanup(self.api.shutdown)

		self.client = ePostClient(
			mock_epost.USERNAME,
			mock_epost.PASSWORD,
			base_url=f"http://127.0.0.1:{self.api.server_address[1]}",
			tenant_id=TENANT_ID,
			company_id=str(COMPANY_ID),
		)
		self.client.BACKOFF_SECONDS = 0
		self.addCleanup(self.client.session.close)

	def test_a_redirect_is_refused_rather_than_followed(self):
		with self.assertRaises(ePostAPIError) as raised:
			self.client.authenticate()

		self.assertIn("redirected", str(raised.exception))
		self.assertEqual(_Recorder.received, [])

	def test_no_non_get_reaches_the_letterbox_by_way_of_a_redirect(self):
		with self.assertRaises(ePostAPIError):
			self.client.authenticate()

		self.assertEqual([(m, p) for m, p, _ in _Recorder.received if p.startswith("/epost/")], [])

	def test_the_password_does_not_travel_to_the_redirect_target(self):
		with self.assertRaises(ePostAPIError):
			self.client.authenticate()

		self.assertNotIn(mock_epost.PASSWORD, "".join(body for _, _, body in _Recorder.received))


class StateIsolationTest(unittest.TestCase):
	def test_two_mocks_do_not_share_fixture_objects(self):
		"""A mutable default would let one test's edits leak into the next."""
		first, second = MockState(), MockState()
		first.inbox[0]["letterTitle"] = "changed"

		self.assertNotEqual(second.inbox[0]["letterTitle"], "changed")


if __name__ == "__main__":
	unittest.main()
