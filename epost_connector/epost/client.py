"""Read-only HTTP client for the KLARA / ePost Public API (api.epost.ch).

HARD CONSTRAINT — this client is read-only toward ePost.

An n8n workflow (`7sytwdFCRMkgSED9`) processes the same letterbox in parallel and
owns the letter lifecycle there. Marking a letter read, accepted, rejected or
archived from here would change what that workflow sees. So:

  * no method for /read, /accept, /reject, /archive, /restore or DELETE exists,
  * `_request` refuses any verb other than GET below /epost/,

which leaves POST available only for the two /core/latest/ auth endpoints.

Endpoint and schema facts below are taken from spec/klara_public_api.yaml
(KLARA Public API, openapi 3.0.3); line numbers are cited where they matter.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from epost_connector.epost.exceptions import (
	ePostAPIError,
	ePostAuthError,
	ePostWriteAttempt,
)

DEFAULT_BASE_URL = "https://api.epost.ch"

TENANTS_PATH = "/core/latest/tenants"
TOKEN_PATH = "/core/latest/token"
LETTERS_PATH = "/epost/v2/letters"


@dataclass
class ePostToken:
	"""An access token plus the wall-clock time it stops being usable."""

	access_token: str
	expires_at: float
	refresh_token: str | None = None
	refresh_expires_at: float | None = None

	def is_expired(self, skew: int = 60) -> bool:
		return time.time() >= (self.expires_at - skew)

	def can_refresh(self, skew: int = 60) -> bool:
		if not self.refresh_token:
			return False
		if self.refresh_expires_at is None:
			return True
		return time.time() < (self.refresh_expires_at - skew)


class ePostClient:
	"""Talks to the ePost Public API on behalf of one tenant/company.

	Constructed with explicit credentials so it can be exercised against a mock
	server without a Frappe site; `from_settings()` is the in-Desk entry point.
	"""

	#: spec:11628-11640 — `limit` maximum is 1000, default 48.
	MAX_PAGE_SIZE = 1000
	DEFAULT_PAGE_SIZE = 200

	#: Stop rather than loop forever if the API ever ignores `offset`.
	MAX_LETTERS = 50_000

	MAX_ATTEMPTS = 3
	BACKOFF_SECONDS = 1.0
	RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

	def __init__(
		self,
		username: str,
		password: str,
		*,
		base_url: str = DEFAULT_BASE_URL,
		tenant_id: str | None = None,
		company_id: str | None = None,
		timeout: int = 60,
		page_size: int = DEFAULT_PAGE_SIZE,
		session: requests.Session | None = None,
	) -> None:
		if not username or not password:
			raise ePostAuthError("ePost username and password are required")

		self.username = username
		self.password = password
		self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
		self.tenant_id = tenant_id
		self.company_id = company_id
		self.timeout = timeout
		self.page_size = min(max(int(page_size), 1), self.MAX_PAGE_SIZE)
		self.session = session or requests.Session()
		self.token: ePostToken | None = None

	@classmethod
	def from_settings(cls, settings: Any = None, **overrides: Any) -> ePostClient:
		"""Build a client from the `ePost Settings` single doc."""
		import frappe

		from epost_connector.epost.exceptions import ePostNotConfigured

		settings = settings or frappe.get_cached_doc("ePost Settings")
		password = settings.get_password("password", raise_exception=False)
		if not settings.username or not password:
			raise ePostNotConfigured("Set the ePost username and password in ePost Settings")

		return cls(
			settings.username,
			password,
			base_url=settings.api_base_url or DEFAULT_BASE_URL,
			tenant_id=settings.tenant_id or None,
			company_id=str(settings.company_id) if settings.company_id else None,
			**overrides,
		)

	# ------------------------------------------------------------------
	# Letters — every call below is a GET
	# ------------------------------------------------------------------

	def list_letters(
		self,
		*,
		limit: int | None = None,
		offset: int = 0,
		letter_types: tuple[str, ...] = ("CLASSIC_LETTER",),
		letter_folder: str = "INBOX_FOLDER",
		read_status: str = "ALL",
		from_date: str | None = None,
		to_date: str | None = None,
	) -> list[dict]:
		"""One page of letters.

		spec:11693-11701 — the 200 response is a bare JSON array of `Letter`,
		with no envelope and no total count, so exhaustion is detected by a
		short page. spec:11622 — `letter-types` is a *required* query param.
		"""
		params: dict[str, Any] = {
			"limit": min(limit or self.page_size, self.MAX_PAGE_SIZE),
			"offset": offset,
			"letter-types": list(letter_types),
			"letter-folder": letter_folder,
			"read-status": read_status,
		}
		if from_date and to_date:
			# spec:11596-11599 — the API only filters when both bounds are given.
			params["from-date"] = from_date
			params["to-date"] = to_date

		return self._get_list(LETTERS_PATH, params=params)

	def iter_letters(self, **filters: Any) -> Iterator[dict]:
		"""Every letter matching `filters`, paginating to exhaustion."""
		page_size = min(filters.pop("page_size", None) or self.page_size, self.MAX_PAGE_SIZE)
		offset = 0
		total = 0

		while True:
			page = self.list_letters(limit=page_size, offset=offset, **filters)
			yield from page
			total += len(page)

			if len(page) < page_size:
				return
			if total >= self.MAX_LETTERS:
				raise ePostAPIError(
					f"Stopped after {total} letters — the API kept returning full pages, "
					"which suggests `offset` is being ignored"
				)
			offset += page_size

	def get_letter(self, letter_id: str) -> dict:
		"""spec:12030-12035 — returns a single `Letter` object."""
		result = self._request("GET", f"{LETTERS_PATH}/{letter_id}").json()
		if not isinstance(result, dict):
			raise ePostAPIError(f"Expected a letter object for {letter_id}, got {type(result).__name__}")
		return result

	def get_letter_content(self, letter_id: str) -> bytes:
		"""The letter PDF. spec:12194-12201 — application/octet-stream."""
		response = self._request(
			"GET",
			f"{LETTERS_PATH}/{letter_id}/content",
			headers={"Accept": "application/octet-stream"},
		)
		if not response.content:
			raise ePostAPIError(f"Letter {letter_id} returned empty content", url=response.url)
		return response.content

	def get_letter_thumbnail(self, letter_id: str) -> bytes:
		"""spec:12308-12309 — JPEG byte stream, 90x128 by default."""
		response = self._request(
			"GET",
			f"{LETTERS_PATH}/{letter_id}/thumbnail",
			headers={"Accept": "application/octet-stream"},
		)
		return response.content

	def search_letters(
		self,
		value: str,
		*,
		limit: int | None = None,
		offset: int = 0,
		search_location: str = "ALL",
	) -> list[dict]:
		"""spec:11981-11989 — same bare `Letter` array as the list endpoint."""
		return self._get_list(
			f"{LETTERS_PATH}/search",
			params={
				"value": value,
				"limit": min(limit or self.page_size, self.MAX_PAGE_SIZE),
				"offset": offset,
				"search-location": search_location,
			},
		)

	def get_unread_count(self) -> int:
		"""spec:11815-11819 — the 200 body is a bare integer."""
		return int(self._request("GET", f"{LETTERS_PATH}/inbox/count").json())

	# ------------------------------------------------------------------
	# Authentication
	# ------------------------------------------------------------------

	def list_tenants(self) -> list[dict]:
		"""Tenants available to these credentials.

		spec:5889-5897 — array of `Tenant` {tenant_id, company_id, company_name}.
		Needs no bearer token; the credentials are the body.
		"""
		response = self._send(
			"POST",
			TENANTS_PATH,
			data={"username": self.username, "password": self.password},
			authenticated=False,
		)
		tenants = self._json(response)
		if not isinstance(tenants, list):
			raise ePostAuthError("Expected a list of tenants", status_code=response.status_code)
		return tenants

	def authenticate(self) -> ePostToken:
		"""Password grant. spec:5984-6020 — form-urlencoded, returns PublicAPIToken."""
		if not self.tenant_id or not self.company_id:
			self._resolve_single_tenant()

		self.token = self._token_request(
			{
				"grant_type": "password",
				"username": self.username,
				"password": self.password,
				"tenant_id": self.tenant_id,
				"company_id": self.company_id,
			}
		)
		return self.token

	def _resolve_single_tenant(self) -> None:
		tenants = self.list_tenants()
		if len(tenants) != 1:
			raise ePostAuthError(
				f"{len(tenants)} tenants available — set tenant_id and company_id in ePost Settings"
			)
		self.tenant_id = tenants[0].get("tenant_id")
		self.company_id = str(tenants[0].get("company_id"))

	def _refresh_token(self) -> ePostToken | None:
		"""Refresh grant. spec:5974-5977 — grant_type=refresh_token."""
		if not self.token or not self.token.can_refresh():
			return None
		try:
			self.token = self._token_request(
				{
					"grant_type": "refresh_token",
					"refresh_token": self.token.refresh_token,
					"tenant_id": self.tenant_id,
					"company_id": self.company_id,
				}
			)
		except ePostAPIError:
			return None
		return self.token

	def _token_request(self, data: dict[str, Any]) -> ePostToken:
		response = self._send("POST", TOKEN_PATH, data=data, authenticated=False)
		payload = self._json(response)
		if not isinstance(payload, dict) or not payload.get("access_token"):
			raise ePostAuthError("Token response carried no access_token", status_code=response.status_code)

		now = time.time()
		expires_in = int(payload.get("expires_in") or 300)
		refresh_expires_in = payload.get("refresh_expires_in")

		# The spec disagrees with itself on the refresh-token key: PublicAPIToken
		# (spec:17747) says `refreshToken`, AccessTokenResponse (spec:12775) says
		# `refresh_token`. Accept either.
		refresh_token = payload.get("refresh_token") or payload.get("refreshToken")

		return ePostToken(
			access_token=payload["access_token"],
			expires_at=now + expires_in,
			refresh_token=refresh_token,
			refresh_expires_at=now + int(refresh_expires_in) if refresh_expires_in else None,
		)

	def _ensure_token(self) -> str:
		if self.token is None:
			self.authenticate()
		elif self.token.is_expired():
			if self._refresh_token() is None:
				self.authenticate()
		return self.token.access_token

	# ------------------------------------------------------------------
	# Transport
	# ------------------------------------------------------------------

	def _get_list(self, path: str, params: dict[str, Any]) -> list[dict]:
		result = self._request("GET", path, params=params).json()
		if not isinstance(result, list):
			raise ePostAPIError(f"Expected a JSON array from {path}, got {type(result).__name__}")
		return result

	def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
		"""Authenticated request, re-authenticating once on a 401."""
		self._ensure_token()
		response = self._send(method, path, **kwargs)

		if response.status_code == 401:
			# The token may have been revoked server-side before it expired.
			self.token = None
			self._ensure_token()
			response = self._send(method, path, **kwargs)

		return self._checked(response)

	def _send(
		self,
		method: str,
		path: str,
		*,
		authenticated: bool = True,
		headers: dict[str, str] | None = None,
		**kwargs: Any,
	) -> requests.Response:
		self._guard_read_only(method, path)

		url = urljoin(self.base_url + "/", path.lstrip("/"))
		request_headers = {"Accept": "application/json"}
		if authenticated and self.token:
			request_headers["Authorization"] = f"Bearer {self.token.access_token}"
		if headers:
			request_headers.update(headers)

		last_error: Exception | None = None
		for attempt in range(1, self.MAX_ATTEMPTS + 1):
			try:
				response = self.session.request(
					method,
					url,
					headers=request_headers,
					timeout=self.timeout,
					**kwargs,
				)
			except requests.RequestException as exc:
				last_error = exc
				response = None

			if response is not None and response.status_code not in self.RETRY_STATUSES:
				return response

			if attempt == self.MAX_ATTEMPTS:
				break
			time.sleep(self.BACKOFF_SECONDS * (2 ** (attempt - 1)))

		if response is not None:
			return response
		raise ePostAPIError(f"{method} {url} failed: {last_error}", url=url)

	@staticmethod
	def _guard_read_only(method: str, path: str) -> None:
		"""Structural half of the read-only contract.

		The n8n workflow owns the letter lifecycle in ePost; a write from here
		would race it. POST stays open only for the /core/latest auth endpoints.
		"""
		if path.startswith("/epost/") and method.upper() != "GET":
			raise ePostWriteAttempt(f"Refusing {method} {path}: this app is read-only toward ePost")

	def _checked(self, response: requests.Response) -> requests.Response:
		if response.ok:
			return response

		message, code = self._error_detail(response)
		error = ePostAuthError if response.status_code in (401, 403) else ePostAPIError
		raise error(message, status_code=response.status_code, code=code, url=response.url)

	@staticmethod
	def _error_detail(response: requests.Response) -> tuple[str, str | None]:
		try:
			payload = response.json()
		except ValueError:
			return (response.text or response.reason or "Request failed")[:500], None

		if not isinstance(payload, dict):
			return str(payload)[:500], None

		# ErrorMessage (spec:16104) uses code/message; the auth endpoints answer
		# with error/error_description instead.
		message = (
			payload.get("message")
			or payload.get("error_description")
			or payload.get("error")
			or response.reason
			or "Request failed"
		)
		return str(message)[:500], payload.get("code") or payload.get("error")

	@staticmethod
	def _json(response: requests.Response) -> Any:
		if not response.ok:
			message, code = ePostClient._error_detail(response)
			raise ePostAuthError(message, status_code=response.status_code, code=code, url=response.url)
		try:
			return response.json()
		except ValueError as exc:
			raise ePostAPIError(f"Response was not JSON: {exc}", url=response.url) from exc
