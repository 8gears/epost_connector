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

import functools
import importlib.util
import json
import re
from typing import Any

import frappe

# The flow app's Model default.
TIMEOUT = 60
FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def ask_json(model_name: str, messages: list[dict], schema: dict) -> tuple[dict | None, dict | None]:
	"""Return (answer, usage). The answer is None when there is no usable JSON object."""
	chat = _chat_function()
	if chat is None:
		frappe.log_error("ePost: Frappe Flow is not installed", reference_doctype="ePost Settings")
		return None, None

	messages = [dict(m) for m in messages]
	messages[0]["content"] = (
		f"{messages[0]['content']}\n\nReply with one JSON object and nothing else. "
		f"It must validate against this JSON Schema:\n{json.dumps(schema)}"
	)

	# A frappe.throw on the way (no permission for the model, model disabled) queues a
	# message for the Desk user before it raises; the failure is swallowed here, so is its dialog.
	message_log = getattr(frappe.local, "message_log", None)
	queued = len(message_log) if message_log is not None else 0
	try:
		content, usage = chat(model_name, messages)
	except Exception:
		if message_log is not None:
			del message_log[queued:]
		frappe.log_error("ePost: model call failed", reference_doctype="ePost Settings")
		return None, None

	return parse_object(content), usage


@functools.cache
def _chat_function():
	"""By presence, not by import: an import that fails inside an installed Flow is a model
	call failure with its traceback, not a missing Flow."""
	if importlib.util.find_spec("frappe.flow"):
		return _chat_frappe_flow
	if importlib.util.find_spec("flow"):
		return _chat_flow_app
	return None


def _chat_frappe_flow(model_name: str, messages: list[dict]) -> tuple[str | None, dict | None]:
	"""frappe's Flow has no chat call outside its daemon, so litellm is called with its config.

	`model_config` checks that the user may read the Flow Model, which honours its Roles
	(frappe/flow/permissions.py has_role_permission). Scheduled jobs run as Administrator,
	who passes every check; a Desk user without access gets no answer, as for any failure."""
	import litellm
	from frappe.flow.lib.model import litellm_route, model_config

	config = model_config(model_name)
	model, api_base = litellm_route(config["model"], config.get("api_base"))
	# The flow app's order: the model's Extra Params override our defaults, timeout included.
	kwargs: dict[str, Any] = {
		"model": model,
		"api_key": config.get("api_key"),
		"messages": messages,
		"timeout": TIMEOUT,
		**config.get("model_params", {}),
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
	return {
		"prompt_tokens": usage.get("prompt_tokens", 0) or 0,
		"completion_tokens": usage.get("completion_tokens", 0) or 0,
	}


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
