"""A stand-in for api.epost.ch, so the app can be exercised without the real account.

Faithful to `spec/klara_public_api.yaml` in the shapes that matter, including the
awkward ones:

  * `/epost/v2/letters` answers with a **bare JSON array**, no envelope and no
	total count (spec:11693-11701), so the end of the listing can only be
	inferred from a short page;
  * `letter-types` is a *required* query parameter (spec:11622) — omitting it is
	a 400 here, as it is there;
  * `/epost/v2/archives/letters` without `directory-id` lists the **root
	storage only** (spec:11370-11375), not every archived letter, so a letter
	filed into a folder is reachable only through that folder's listing;
  * `directoryId` is empty for the branded pseudo-directory (spec:15291);
  * `/epost/v2/letters/inbox/count` answers with a bare integer (spec:11815).

It also ships the answers a happy mock never produces, because every one of them
has been seen from a real gateway and each is indistinguishable from success
until the body is inspected: a content endpoint answering 200 with an HTML error
page, the same endpoint answering 200 with nothing at all, a service that
ignores `offset` and re-serves the first window forever, a folder name that
arrives NFD-decomposed, and a field whose value is shaped like a filesystem path.

Nothing here writes: the mock refuses any verb but GET below `/epost/`, which is
the same contract `ePostClient._guard_read_only` enforces from the other side.

Usage:

	with MockePost() as mock:
		client = ePostClient(mock.USERNAME, mock.PASSWORD, base_url=mock.base_url)
"""

from __future__ import annotations

import base64
import json
import threading
import unicodedata
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------------
# Credentials and constants the tests assert against
# ----------------------------------------------------------------------

USERNAME = "letterbox@example.com"

#: Long enough to trip `ePostClient._redact`, which leaves values under eight
#: characters alone so that ordinary short words are not mangled. A password a
#: test cannot get redacted proves nothing about redaction.
PASSWORD = "correct-horse-battery-staple"

ACCESS_TOKEN = "tok-abcdefghijklmnopqrstuvwxyz0123456789"
REFRESHED_ACCESS_TOKEN = "tok-9876543210zyxwvutsrqponmlkjihgfedcba"
REFRESH_TOKEN = "ref-abcdefghijklmnopqrstuvwxyz0123456789"

TENANT_ID = "t-1"
COMPANY_ID = 42
COMPANY_NAME = "8gears AG"

#: spec:11628-11640 — `limit` defaults to 48 and maxes out at 1000.
DEFAULT_LIMIT = 48
MAX_LIMIT = 1000

#: Written decomposed (u + combining diaeresis) exactly as the service sends it.
NFD_DIRECTORY_NAME = unicodedata.normalize("NFD", "Bürö")
NFC_DIRECTORY_NAME = unicodedata.normalize("NFC", "Bürö")

#: A value shaped like a path. The app builds a stored file name out of service
#: strings, so a service that says this must not get to decide where a file lands.
TRAVERSAL = "../../../../tmp/pwned"

#: Letter ids the hostile fixtures live under, so tests can name them.
HTML_CONTENT_LETTER = "inbox-html-error"
EMPTY_CONTENT_LETTER = "inbox-empty-body"
BAD_THUMBNAIL_LETTER = "inbox-bad-thumbnail"
TRAVERSAL_DATE_LETTER = "inbox-traversal-date"
TRAVERSAL_TITLE_LETTER = "inbox-traversal-title"
ECHO_SECRET_LETTER = "echo-secret"

#: A real 1x1 JPEG. The thumbnail goes into an Attach Image field, so Frappe
#: hands it to an image library — a byte string that merely starts with the JFIF
#: magic is not enough, and a fixture that is not decodable tests the error path
#: rather than the happy one.
JPEG_1X1 = base64.b64decode(
	"/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
	"HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAHwAAAQUBAQEB"
	"AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
	"ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
	"WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
	"x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AD9/oooor//Z"
)

#: The same gateway error page, but on the thumbnail endpoint. It arrives as a
#: 200, so only a decode attempt can tell — and that attempt happens inside
#: Frappe's File insert, which is why it is a fixture and not a unit test.
HTML_ERROR_PAGE = b"<html><body><h1>502 Bad Gateway</h1></body></html>\n"

DIRECTORIES = [
	{
		"directoryId": "dir-one",
		"directoryName": "Rechnungen",
		"numberOfDocuments": 1,
		"hasSubDirectories": False,
	},
	{
		"directoryId": "dir-two",
		"directoryName": NFD_DIRECTORY_NAME,
		"numberOfDocuments": 1,
		"hasSubDirectories": False,
	},
	# spec:15291 — the branded directory has no id. Its documents come back in
	# the root listing, so a client must skip it rather than list it by id.
	{
		"directoryId": "",
		"directoryName": "ePost Scancenter",
		"numberOfDocuments": 0,
		"hasSubDirectories": False,
	},
]

AUTH_PATHS = ("/core/latest/tenants", "/core/latest/token")


def letter(letter_id: str, **over: Any) -> dict:
	"""One `Letter` object (spec:16524).

	Note what is *not* here: the schema has no `senderName`. Whatever carries the
	sender arrives in `description` ("Invoice from <sender>") or as an opaque
	participant id, which is why the app has to fall back through both.
	"""
	payload = {
		"id": letter_id,
		"letterTitle": "Gescannter Brief",
		"fileName": f"{letter_id}.pdf",
		"senderParticipantId": "b0f742a3-0a54-401d-bf41-38a3a9628953",
		"senderUserId": "84332810",
		"documentTypes": ["invoice"],
		"letterContentReference": f"https://api.epost.ch/epost/v2/letters/{letter_id}/content",
		"letterType": "CLASSIC_LETTER",
		"receivedDateTime": "2021-09-29T04:21:10.163Z",
		"description": "Invoice from Muster Elektro AG",
		"readStatus": "UNREAD",
	}
	payload.update(over)
	return payload


def default_inbox() -> list[dict]:
	return [
		letter("inbox-1"),
		# No description at all: the sender has to come from somewhere else.
		letter("inbox-2", description=None, readStatus="READ", letterTitle="Kontoauszug"),
		letter(TRAVERSAL_DATE_LETTER, receivedDateTime=TRAVERSAL),
		letter(TRAVERSAL_TITLE_LETTER, letterTitle=TRAVERSAL, fileName=f"{TRAVERSAL}.pdf"),
		letter(HTML_CONTENT_LETTER, letterTitle="Gateway hiccup"),
		letter(EMPTY_CONTENT_LETTER, letterTitle="Truncated"),
		letter(BAD_THUMBNAIL_LETTER, letterTitle="Good PDF, undecodable preview"),
	]


def minimal_pdf(letter_id: str) -> bytes:
	"""A structurally valid one-page PDF carrying `letter_id` in its title.

	It has to be a real PDF, not a body that merely starts with `%PDF-`: Frappe
	parses an attached PDF when the File row is inserted, and a malformed one
	fails the whole download with `startxref not found`. The offsets are computed
	rather than written out, because a hand-counted xref goes stale the moment
	anything above it changes length.

	The id goes into the bytes so that a download which fetched the wrong letter
	is distinguishable from a correct one.
	"""
	title = "".join(c for c in letter_id if c.isalnum() or c in "-_ ")
	objects = [
		b"<< /Type /Catalog /Pages 2 0 R >>",
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
		f"<< /Title ({title}) >>".encode(),
	]

	out = bytearray(b"%PDF-1.4\n")
	offsets: list[int] = []
	for number, body in enumerate(objects, start=1):
		offsets.append(len(out))
		out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

	xref_at = len(out)
	out += f"xref\n0 {len(objects) + 1}\n".encode()
	out += b"0000000000 65535 f \n"
	for offset in offsets:
		out += f"{offset:010d} 00000 n \n".encode()
	out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 4 0 R >>\n".encode()
	out += f"startxref\n{xref_at}\n%%EOF\n".encode()
	return bytes(out)


def default_content_override() -> dict[str, tuple[bytes, str]]:
	"""The two answers that are indistinguishable from success until inspected.

	Hostile by default rather than opt-in: a suite that has to remember to switch
	these on is a suite that will forget, and both have been seen from a real
	gateway. Clear the dict for a run in which every letter downloads.
	"""
	return {
		# 200, with an error page in the body. Stored under a letter's name it is
		# indistinguishable from the letter afterwards.
		HTML_CONTENT_LETTER: (HTML_ERROR_PAGE, "text/html"),
		# 200, and nothing in it. A gateway that truncates, or a 204 — identical
		# from here, and the one answer a length check cannot catch, because it
		# flags a bad answer by its length and zero is a falsy length.
		EMPTY_CONTENT_LETTER: (b"", "application/octet-stream"),
	}


def default_thumbnail_override() -> dict[str, tuple[bytes, str]]:
	"""One letter whose preview is an error page while its PDF is perfectly fine.

	Hostile by default, because the failure it produces does not look like a
	thumbnail failure: the decode happens inside Frappe's File insert, well
	after the letter's own download succeeded.
	"""
	return {BAD_THUMBNAIL_LETTER: (HTML_ERROR_PAGE, "text/html")}


def default_archive() -> list[dict]:
	return [
		letter("arch-1", letterTitle="Rechnung 2024-01"),
		letter("arch-2", letterTitle="Unfiled", description=None),
		letter("arch-3", letterTitle="Ordner mit Umlaut"),
	]


@dataclass
class MockState:
	"""Everything a test can bend, and everything the server recorded."""

	inbox: list[dict] = field(default_factory=default_inbox)
	archive: list[dict] = field(default_factory=default_archive)
	#: A token is only ever valid for one tenant/company pair, so an account
	#: with more than one of them cannot be resolved without being told which.
	tenants: list[dict] = field(
		default_factory=lambda: [
			{"tenant_id": TENANT_ID, "company_id": COMPANY_ID, "company_name": COMPANY_NAME}
		]
	)
	directories: list[dict] = field(default_factory=lambda: [dict(d) for d in DIRECTORIES])
	#: directory id -> letter ids filed into it. Anything not listed here sits in
	#: root storage and is only reachable through the directory-less listing.
	in_folder: dict[str, list[str]] = field(
		default_factory=lambda: {"dir-one": ["arch-1"], "dir-two": ["arch-3"]}
	)

	#: `(method, path, query)` for every request, in order.
	calls: list[tuple[str, str, dict]] = field(default_factory=list)
	#: The decoded form body of every `/core/latest/token` request.
	token_requests: list[dict] = field(default_factory=list)

	#: letter id -> (body, content-type) served by `/content` instead of a PDF.
	content_override: dict[str, tuple[bytes, str]] = field(default_factory=default_content_override)
	#: The same, for `/thumbnail`.
	thumbnail_override: dict[str, tuple[bytes, str]] = field(default_factory=default_thumbnail_override)

	#: Statuses to answer the next letterbox calls with, consumed one per call.
	#: An entry without a `path` never matches the two auth endpoints, so a
	#: queued failure is not eaten by the re-authentication it provokes.
	force_status: list[dict] = field(default_factory=list)

	#: A service that documents `offset` and then ignores it, re-serving the
	#: first window forever. Which of the two the real one is, is unknown.
	ignore_offset: bool = False

	#: spec:11815-11819 says the inbox count is a bare integer; a working
	#: reference client observed `{"count": n}`. Neither can be confirmed from
	#: here, so the client accepts both and this switches between them.
	count_as_object: bool = False

	#: Wrap a listing in the envelope the rest of this API does not use. The spec
	#: says a bare array, but a service that grew a wrapper must not be read as an
	#: empty letterbox — which is what "no envelope handling" would mean.
	list_envelope: str | None = None

	#: Token lifetime handed out. Small values make the client re-authenticate.
	expires_in: int = 600
	refresh_expires_in: int | None = 1800
	#: When off, the refresh grant is rejected and the client must fall back to
	#: the password grant.
	accept_refresh_grant: bool = True
	#: Bearer tokens the server has stopped honouring, to model a revocation
	#: that happened before the token's own expiry.
	revoked_tokens: set[str] = field(default_factory=set)
	#: Answer this many authenticated letterbox calls with 401 whatever the
	#: token says, then start honouring them again. Models a token revoked
	#: server-side before it expired: the client cannot see it coming and the
	#: only way through is to re-authenticate and try the call once more.
	reject_bearer_times: int = 0

	def issued_tokens(self) -> list[str]:
		return [t.get("grant_type") for t in self.token_requests]

	def calls_to(self, path: str) -> list[tuple[str, str, dict]]:
		return [c for c in self.calls if c[1] == path]

	def content_for(self, letter_id: str) -> tuple[bytes, str]:
		if letter_id in self.content_override:
			return self.content_override[letter_id]
		return (minimal_pdf(letter_id), "application/octet-stream")

	def thumbnail_for(self, letter_id: str) -> tuple[bytes, str]:
		if letter_id in self.thumbnail_override:
			return self.thumbnail_override[letter_id]
		# spec:12308 — a JPEG byte stream, 90x128 by default.
		return (JPEG_1X1, "image/jpeg")

	def all_letters(self) -> list[dict]:
		return [*self.inbox, *self.archive]

	def root_storage(self) -> list[dict]:
		filed = {i for ids in self.in_folder.values() for i in ids}
		return [letter_ for letter_ in self.archive if letter_.get("id") not in filed]

	def in_directory(self, directory_id: str) -> list[dict]:
		ids = self.in_folder.get(directory_id, [])
		return [letter_ for letter_ in self.archive if letter_.get("id") in ids]


class _Handler(BaseHTTPRequestHandler):
	protocol_version = "HTTP/1.1"
	#: Headers and body go out as two separate writes, and without TCP_NODELAY
	#: the second one waits on a delayed ACK — about 35ms per request, on the
	#: loopback, for every call the suite makes.
	disable_nagle_algorithm = True

	# --- plumbing ---------------------------------------------------

	@property
	def state(self) -> MockState:
		return self.server.state  # type: ignore[attr-defined]

	def log_message(self, *args: Any) -> None:
		pass

	def _send(self, code: int, body: Any, content_type: str = "application/json") -> None:
		if isinstance(body, bytes):
			payload = body
		elif isinstance(body, str):
			payload = body.encode()
		else:
			payload = json.dumps(body).encode()

		self.send_response(code)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def _form(self) -> dict[str, str]:
		length = int(self.headers.get("Content-Length") or 0)
		raw = self.rfile.read(length).decode() if length else ""
		return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

	def _int_param(self, query: dict, name: str, default: int) -> int:
		try:
			return int(query[name][0])
		except (KeyError, IndexError, ValueError):
			return default

	# --- routing ----------------------------------------------------

	def do_GET(self) -> None:
		self._route("GET")

	def do_POST(self) -> None:
		self._route("POST")

	def do_PATCH(self) -> None:
		self._route("PATCH")

	def do_DELETE(self) -> None:
		self._route("DELETE")

	def do_PUT(self) -> None:
		self._route("PUT")

	def _route(self, method: str) -> None:
		parsed = urlparse(self.path)
		path = parsed.path
		query = parse_qs(parsed.query, keep_blank_values=True)
		self.state.calls.append((method, path, query))

		if path in AUTH_PATHS:
			self._auth_route(method, path)
			return

		# The read-only contract from the server's side. The n8n workflow owns
		# the letter lifecycle in ePost; a write from here would race it, so the
		# mock refuses to make one succeed even if the client tried.
		if path.startswith("/epost/") and method != "GET":
			self._send(405, {"error": "method_not_allowed", "message": f"{method} {path} is not read-only"})
			return

		forced = self._take_forced(path)
		if forced is not None:
			self._send(forced, {"error": "forced", "path": path})
			return

		if self.state.reject_bearer_times > 0:
			self.state.reject_bearer_times -= 1
			self._send(401, {"error": "unauthorized", "error_description": "token revoked"})
			return

		if not self._authorised():
			self._send(401, {"error": "unauthorized", "error_description": "invalid or expired token"})
			return

		self._letterbox_route(path, query)

	def _take_forced(self, path: str) -> int | None:
		for index, entry in enumerate(self.state.force_status):
			if entry.get("path", path) == path:
				return self.state.force_status.pop(index)["status"]
		return None

	def _authorised(self) -> bool:
		header = self.headers.get("Authorization") or ""
		if not header.startswith("Bearer "):
			return False
		token = header[len("Bearer ") :]
		if token in self.state.revoked_tokens:
			return False
		return token in (ACCESS_TOKEN, REFRESHED_ACCESS_TOKEN)

	# --- /core/latest ------------------------------------------------

	def _auth_route(self, method: str, path: str) -> None:
		if method != "POST":
			self._send(405, {"error": "method_not_allowed"})
			return

		form = self._form()

		if path == "/core/latest/tenants":
			# The grant used to accept anything at all, so a client that sent no
			# credentials still passed every authentication test and failed
			# against the real service.
			if form.get("username") != USERNAME or form.get("password") != PASSWORD:
				self._send(400, {"error": "invalid_request", "error_description": "bad credentials"})
				return
			self._send(200, self.state.tenants)
			return

		self.state.token_requests.append(form)
		grant = form.get("grant_type")

		if grant == "refresh_token":
			if not self.state.accept_refresh_grant or form.get("refresh_token") != REFRESH_TOKEN:
				self._send(400, {"error": "invalid_grant", "error_description": "refresh token rejected"})
				return
			self._send(200, self._token_body(REFRESHED_ACCESS_TOKEN))
			return

		if grant != "password":
			self._send(400, {"error": "unsupported_grant_type"})
			return
		if form.get("username") != USERNAME or form.get("password") != PASSWORD:
			self._send(400, {"error": "invalid_request", "error_description": "bad credentials"})
			return
		if form.get("tenant_id") != TENANT_ID or form.get("company_id") != str(COMPANY_ID):
			self._send(400, {"error": "invalid_tenant", "error_description": "unknown tenant/company pair"})
			return

		self._send(200, self._token_body(ACCESS_TOKEN))

	def _token_body(self, access_token: str) -> dict:
		body = {
			"access_token": access_token,
			"token_type": "Bearer",
			"expires_in": self.state.expires_in,
			"refresh_token": REFRESH_TOKEN,
		}
		if self.state.refresh_expires_in is not None:
			body["refresh_expires_in"] = self.state.refresh_expires_in
		return body

	# --- /epost/v2 ---------------------------------------------------

	def _letterbox_route(self, path: str, query: dict) -> None:
		state = self.state

		if path == "/epost/v2/letters":
			# spec:11622 — `letter-types` is required, not merely defaulted.
			if not query.get("letter-types"):
				self._send(400, {"code": "MISSING_PARAM", "message": "letter-types is required"})
				return
			self._send(200, self._listing(state.inbox, query))
			return

		if path == "/epost/v2/letters/inbox/count":
			unread = sum(1 for x in state.inbox if x.get("readStatus") == "UNREAD")
			self._send(200, {"count": unread} if state.count_as_object else unread)
			return

		if path == "/epost/v2/letters/search":
			needle = (query.get("value") or query.get("keyword") or [""])[0].lower()
			hits = [x for x in state.all_letters() if needle in json.dumps(x).lower()]
			self._send(200, self._listing(hits, query))
			return

		if path == "/epost/v2/archives/directories":
			self._send(200, state.directories)
			return

		if path == "/epost/v2/archives/letters":
			directory_id = (query.get("directory-id") or [""])[0]
			pool = state.in_directory(directory_id) if directory_id else state.root_storage()
			self._send(200, self._listing(pool, query))
			return

		if path.startswith("/epost/v2/letters/"):
			self._letter_route(path[len("/epost/v2/letters/") :])
			return

		self._send(404, {"code": "NOT_FOUND", "message": f"no route for {path}"})

	def _letter_route(self, tail: str) -> None:
		letter_id, _, action = tail.partition("/")

		# A gateway that quotes the request it rejected, credentials and all.
		# Real proxies do this, and the body ends up in an ePost Sync Log row.
		if letter_id == ECHO_SECRET_LETTER:
			self._send(
				500,
				{
					"code": "UPSTREAM_REJECTED",
					"message": f"upstream rejected form: password={PASSWORD} authorization=Bearer {ACCESS_TOKEN}",
				},
			)
			return

		# `.get`, because a fixture may deliberately be a letter with no id at all.
		known = next((x for x in self.state.all_letters() if x.get("id") == letter_id), None)
		if known is None:
			self._send(404, {"code": "NOT_FOUND", "message": f"no letter {letter_id}"})
			return

		if action == "content":
			body, content_type = self.state.content_for(letter_id)
			self._send(200, body, content_type)
			return

		if action == "thumbnail":
			body, content_type = self.state.thumbnail_for(letter_id)
			self._send(200, body, content_type)
			return

		if not action:
			self._send(200, known)
			return

		self._send(404, {"code": "NOT_FOUND", "message": f"no action {action}"})

	def _listing(self, pool: list[dict], query: dict):
		"""The window, bare or wrapped depending on `state.list_envelope`."""
		window = self._window(pool, query)
		return {self.state.list_envelope: window} if self.state.list_envelope else window

	def _window(self, pool: list[dict], query: dict) -> list[dict]:
		"""The slice `limit`/`offset` asks for.

		`limit` is honoured because the client reads the edge of the window it
		asked for as the edge of the letterbox. A mock that handed back
		everything however little was asked for could never produce a full page,
		so "there are no more letters" and "this answer was full" would be
		indistinguishable — and the second is the one that truncates silently.
		"""
		limit = min(max(self._int_param(query, "limit", DEFAULT_LIMIT), 1), MAX_LIMIT)
		offset = 0 if self.state.ignore_offset else max(self._int_param(query, "offset", 0), 0)
		return pool[offset : offset + limit]


class _Server(ThreadingHTTPServer):
	daemon_threads = True
	allow_reuse_address = True
	#: `ThreadingMixIn.server_close` otherwise joins every handler thread, and a
	#: handler serving an HTTP/1.1 keep-alive connection sits blocked reading the
	#: next request until the client's socket timeout expires. That turned a
	#: teardown into seconds of waiting per test.
	block_on_close = False

	def __init__(self, address, handler, state: MockState):
		self.state = state
		super().__init__(address, handler)


class MockePost:
	"""A threaded mock of api.epost.ch bound to a free port on the loopback.

	Use as a context manager, or call `start()` / `stop()` yourself.
	"""

	USERNAME = USERNAME
	PASSWORD = PASSWORD

	def __init__(self, state: MockState | None = None) -> None:
		self.state = state or MockState()
		self._server = _Server(("127.0.0.1", 0), _Handler, self.state)
		self._thread: threading.Thread | None = None

	@property
	def port(self) -> int:
		return self._server.server_address[1]

	@property
	def base_url(self) -> str:
		return f"http://127.0.0.1:{self.port}"

	def start(self) -> MockePost:
		self._thread = threading.Thread(target=self._server.serve_forever, name="mock-epost", daemon=True)
		self._thread.start()
		return self

	def stop(self) -> None:
		self._server.shutdown()
		self._server.server_close()
		if self._thread:
			self._thread.join(timeout=5)

	def __enter__(self) -> MockePost:
		return self.start()

	def __exit__(self, *exc: Any) -> None:
		self.stop()
