"""Domain exceptions for the ePost integration.

These are plain exceptions on purpose: the client module stays importable and
testable without a Frappe site. Callers inside Frappe translate them into
`frappe.throw` where a user-facing message is wanted.
"""

from __future__ import annotations


class ePostError(Exception):
	"""Base class for every failure raised by this app's ePost layer."""


class ePostNotConfigured(ePostError):
	"""ePost Settings is missing credentials, or sync is disabled."""


class ePostAPIError(ePostError):
	"""The ePost API returned a response we cannot use."""

	def __init__(
		self,
		message: str,
		*,
		status_code: int | None = None,
		code: str | None = None,
		url: str | None = None,
	) -> None:
		super().__init__(message)
		self.message = message
		self.status_code = status_code
		self.code = code
		self.url = url

	def __str__(self) -> str:
		parts = [self.message]
		if self.status_code is not None:
			parts.append(f"HTTP {self.status_code}")
		if self.code:
			parts.append(f"code={self.code}")
		if self.url:
			parts.append(self.url)
		return " | ".join(parts)


class ePostAuthError(ePostAPIError):
	"""Authentication failed, or the token could not be renewed."""


class ePostWriteAttempt(ePostError):
	"""A state-changing call toward ePost was attempted and blocked.

	This should never be raised in production. It exists so that the read-only
	contract fails loudly during development instead of silently mutating the
	letterbox that the n8n workflow also processes.
	"""
