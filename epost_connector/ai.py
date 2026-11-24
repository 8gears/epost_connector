"""One JSON answer from a Frappe Flow model.

frappe >= 16.60 has Flow built in; on older frappe it is the optional `flow`
app. Either may be missing or fail, so everything here degrades to `None`
rather than raising: a letter the model could not read is the same outcome as a
letter no model was asked about.

The schema goes into the prompt instead of `response_format`. litellm 1.83.7, the
last the flow app allows, emulates `response_format` for Anthropic models it does
not list as supporting structured output by forcing a tool call, and newer models
refuse that with "tool_choice: type "tool" and "any" are not supported for this
model". frappe 16.60.2 pins litellm 1.103.2, not checked for this. Every caller
validates the answer against what it may contain anyway, so a prompt-level schema
loses nothing.
"""

from __future__ import annotations

import importlib
import json
import re
from typing import Any

import frappe

# The flow app's Model default.
TIMEOUT = 60
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# A ModuleNotFoundError naming one of these means that Flow is not installed; any other
# name means it is installed and broken. A leftover frappe/flow directory from a newer
# frappe fails on one of these names too, so it does not hide the flow app.
FRAPPE_FLOW = ("frappe.flow", "frappe.flow.lib", "frappe.flow.lib.model")
FLOW_APP = ("flow", "flow.lib", "flow.lib.model")


def ask_json(model_name: str, messages: list[dict], schema: dict) -> tuple[dict | None, dict | None]:
	"""Return (answer, usage). The answer is None when there is no usable JSON object."""
	messages = [dict(m) for m in messages]
	messages[0]["content"] = (
		f"{messages[0]['content']}\n\nReply with one JSON object and nothing else. "
		f"It must validate against this JSON Schema:\n{json.dumps(schema)}"
	)

	try:
		chat = _chat_function()
		if chat is None:
			frappe.log_error("ePost: Frappe Flow is not installed", reference_doctype="ePost Settings")
			return None, None
		content, usage = _muted(chat, model_name, messages)
	except Exception:
		frappe.log_error("ePost: model call failed", reference_doctype="ePost Settings")
		return None, None

	return parse_object(content), usage


def _chat_function():
	if _installed("frappe.flow.lib.model", FRAPPE_FLOW):
		return _chat_frappe_flow
	if _installed("flow.lib.model", FLOW_APP):
		return _chat_flow_app
	return None


def _installed(module: str, absent_names: tuple[str, ...]) -> bool:
	try:
		importlib.import_module(module)
	except ModuleNotFoundError as exc:
		if exc.name not in absent_names:
			raise
		return False
	return True


def _muted(chat, model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	"""A frappe.throw on the way (a disabled Flow Model) would queue an error dialog for the
	Desk user, but ask_json swallows and logs the failure, so the dialog would be stale."""
	muted = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	try:
		return chat(model_name, messages)
	finally:
		frappe.flags.mute_messages = muted


def _chat_frappe_flow(model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	"""frappe's Flow has no chat call outside its daemon, so litellm is called with its config.

	The Flow Model permission check is off: the model is the one ePost Settings names, not
	the caller's choice, and every caller has already checked its own right to act (write on
	the letter, or frappe.only_for). The flow app checked no permission either. With the
	check on, a Desk-triggered job runs as the clicking user and an Accounts user without
	read on Flow Model would get no answer."""
	import litellm
	from frappe.flow.lib.model import litellm_route, model_config

	config = model_config(model_name, check_permission=False)
	model, api_base = litellm_route(config["model"], config.get("api_base"))
	# Extra Params first, as frappe's FlowModel.test_connection does, so they cannot replace
	# the model, key, messages or timeout.
	kwargs: dict[str, Any] = {
		**(config.get("model_params") or {}),
		"model": model,
		"api_key": config.get("api_key"),
		"messages": messages,
		"timeout": TIMEOUT,
	}
	if api_base:
		kwargs["api_base"] = api_base
	response = litellm.completion(**kwargs)
	return response.choices[0].message.content, _usage(getattr(response, "usage", None))


def _chat_flow_app(model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	from flow.lib.model import Model

	response = Model(model_name).chat(messages)
	return response.content, _usage(getattr(response, "usage", None))


def _usage(usage: Any) -> dict | None:
	if not usage:
		return None
	return {key: _token_count(usage, key) for key in ("prompt_tokens", "completion_tokens")}


def _token_count(usage: Any, key: str) -> int:
	value = usage.get(key) if hasattr(usage, "get") else getattr(usage, key, None)
	return value or 0


def parse_object(content: str | None) -> dict | None:
	"""The JSON object in `content`, tolerating a Markdown fence around it."""
	if not content:
		return None
	text = FENCE.sub("", content.strip())
	start, end = text.find("{"), text.rfind("}")
	if start < 0 or end < start:
		return None
	try:
		value: Any = json.loads(text[start : end + 1])
	except ValueError:
		return None
	return value if isinstance(value, dict) else None
