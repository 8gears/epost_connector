"""Which Flow `ask_json` calls: frappe's built-in one, the separate `flow` app, or none.

The modules are faked in `sys.modules`, so no model is called and the test holds
whether or not the bench has frappe >= 16.60 or the `flow` app installed.
"""

from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from epost_connector import ai

SCHEMA = {"type": "object"}
MESSAGES = [{"role": "user", "content": "Read this letter."}]


def _package(name: str) -> ModuleType:
	module = ModuleType(name)
	module.__path__ = []
	return module


def _frappe_flow(completion: MagicMock, model_params: dict | None = None) -> dict[str, ModuleType]:
	model = ModuleType("frappe.flow.lib.model")
	model.model_config = MagicMock(
		return_value={
			"model": "anthropic/m",
			"api_key": "k",
			"api_base": None,
			"model_params": model_params or {"temperature": 0},
		}
	)
	model.litellm_route = lambda model_id, api_base: (model_id, api_base)
	lib, flow = _package("frappe.flow.lib"), _package("frappe.flow")
	lib.model, flow.lib = model, lib
	return {
		"frappe.flow": flow,
		"frappe.flow.lib": lib,
		"frappe.flow.lib.model": model,
		"litellm": SimpleNamespace(completion=completion),
	}


def _flow_app(model_class: MagicMock) -> dict[str, ModuleType]:
	model = ModuleType("flow.lib.model")
	model.Model = model_class
	lib, flow = _package("flow.lib"), _package("flow")
	lib.model, flow.lib = model, lib
	return {"flow": flow, "flow.lib": lib, "flow.lib.model": model}


NO_FRAPPE_FLOW = dict.fromkeys(("frappe.flow", "frappe.flow.lib", "frappe.flow.lib.model"))
NO_FLOW_APP = dict.fromkeys(("flow", "flow.lib", "flow.lib.model"))


class _BrokenInside(MetaPathFinder, Loader):
	"""frappe.flow.lib.model exists, but a module it imports is missing."""

	def find_spec(self, name, path, target=None):
		return ModuleSpec(name, self) if name == "frappe.flow.lib.model" else None

	def create_module(self, spec):
		return None

	def exec_module(self, module):
		raise ModuleNotFoundError("No module named 'pydantic_core'", name="pydantic_core")


@contextmanager
def _flow_modules(fakes: dict):
	"""Only these Flow modules, none of the real ones a frappe >= 16.60 bench has loaded.

	Real Flow code run in here would import doctype modules that patch.dict drops on
	exit; their classes are then loaded twice, and frappe's pickled caches fail on them."""
	with patch.dict(sys.modules, fakes):
		for name in (*ai.FRAPPE_FLOW, *ai.FLOW_APP):
			if name not in fakes:
				sys.modules.pop(name, None)
		yield


def _reply(content: str, usage=None) -> SimpleNamespace:
	usage = usage or {"prompt_tokens": 7, "completion_tokens": 3}
	return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class AskJsonTest(IntegrationTestCase):
	def test_frappe_flow_is_preferred(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		app_model = MagicMock()
		modules = _frappe_flow(completion)
		with _flow_modules({**modules, **_flow_app(app_model)}):
			answer, got = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		self.assertEqual(got, {"prompt_tokens": 7, "completion_tokens": 3})
		# Desk jobs run as the clicking user, who need not have read on Flow Model.
		modules["frappe.flow.lib.model"].model_config.assert_called_once_with(
			"Claude", check_permission=False
		)
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["model"], kwargs["api_key"], kwargs["temperature"]), ("anthropic/m", "k", 0))
		self.assertEqual(kwargs["timeout"], ai.TIMEOUT)
		self.assertNotIn("api_base", kwargs)
		app_model.assert_not_called()

	def test_extra_params_cannot_override_timeout(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		extra = {"timeout": 5, "model": "other", "temperature": 0, "top_p": 0.5}
		with _flow_modules(_frappe_flow(completion, extra)):
			answer, _ = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})
		kwargs = completion.call_args.kwargs
		self.assertEqual((kwargs["timeout"], kwargs["model"]), (ai.TIMEOUT, "anthropic/m"))
		self.assertEqual((kwargs["temperature"], kwargs["top_p"]), (0, 0.5))

	def test_usage_as_attributes(self):
		usage = SimpleNamespace(prompt_tokens=11, completion_tokens=None)
		completion = MagicMock(return_value=_reply('{"a": 1}', usage))
		with _flow_modules(_frappe_flow(completion)):
			_, got = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(got, {"prompt_tokens": 11, "completion_tokens": 0})

	def test_failed_model_call_queues_no_message(self):
		completion = MagicMock()
		modules = _frappe_flow(completion)
		muted_during_call = []

		def disabled(*args, **kwargs):
			muted_during_call.append(ai.frappe.flags.mute_messages)
			raise ValueError("Flow Model Claude is disabled.")

		modules["frappe.flow.lib.model"].model_config.side_effect = disabled
		with (
			_flow_modules(modules),
			patch.dict(ai.frappe.flags, {"mute_messages": False}),
			patch.object(ai.frappe, "log_error") as log,
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
			self.assertFalse(ai.frappe.flags.mute_messages)
		self.assertEqual(muted_during_call, [True])
		log.assert_called_once()
		completion.assert_not_called()

	def test_broken_frappe_flow_is_a_logged_failure(self):
		app_model = MagicMock()
		modules = {
			"frappe.flow": _package("frappe.flow"),
			"frappe.flow.lib": _package("frappe.flow.lib"),
			**_flow_app(app_model),
		}
		traceback = []
		with (
			_flow_modules(modules),
			patch.object(sys, "meta_path", [_BrokenInside(), *sys.meta_path]),
			patch.object(
				ai.frappe, "log_error", side_effect=lambda *a, **k: traceback.append(sys.exc_info()[1])
			),
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
		self.assertEqual(len(traceback), 1)
		self.assertEqual(traceback[0].name, "pydantic_core")
		app_model.assert_not_called()

	def test_leftover_frappe_flow_directory_falls_back_to_flow_app(self):
		app_model = MagicMock()
		app_model.return_value.chat.return_value = SimpleNamespace(content='{"b": 2}', usage=None)
		stale = {"frappe.flow": _package("frappe.flow"), "frappe.flow.lib": None}
		with _flow_modules({**stale, **_flow_app(app_model)}):
			answer, _ = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"b": 2})
		app_model.assert_called_once_with("Claude")

	def test_module_without_spec_does_not_raise(self):
		completion = MagicMock(return_value=_reply('{"a": 1}'))
		modules = _frappe_flow(completion)
		# importlib.util.find_spec raises ValueError on such a module.
		self.assertIsNone(modules["frappe.flow"].__spec__)
		with _flow_modules(modules):
			answer, _ = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"a": 1})

	def test_frappe_flow_api_matches(self):
		"""The real module, where the bench has frappe >= 16.60: the names and arguments ai.py uses."""
		try:
			from frappe.flow.lib.model import litellm_route, model_config
		except ImportError:
			self.skipTest("frappe has no built-in Flow")
		params = inspect.signature(model_config).parameters
		self.assertEqual(params["check_permission"].kind, inspect.Parameter.KEYWORD_ONLY)
		self.assertEqual(list(inspect.signature(litellm_route).parameters), ["model_id", "api_base"])

	def test_flow_app_when_frappe_has_no_flow(self):
		app_model = MagicMock()
		app_model.return_value.chat.return_value = SimpleNamespace(
			content='{"b": 2}', usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
		)
		with _flow_modules({**NO_FRAPPE_FLOW, **_flow_app(app_model)}):
			answer, usage = ai.ask_json("Claude", MESSAGES, SCHEMA)
		self.assertEqual(answer, {"b": 2})
		self.assertEqual(usage, {"prompt_tokens": 1, "completion_tokens": 2})
		app_model.assert_called_once_with("Claude")

	def test_no_flow_at_all(self):
		with (
			_flow_modules({**NO_FRAPPE_FLOW, **NO_FLOW_APP}),
			patch.object(ai.frappe, "log_error") as log,
		):
			self.assertEqual(ai.ask_json("Claude", MESSAGES, SCHEMA), (None, None))
		log.assert_called_once()
