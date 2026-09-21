"""Native binding invariants for anonymous registered TUI handlers."""

from types import SimpleNamespace

import pytest

from tui_gateway.method_ctx import HandlerRegistry, bind_module


def _anonymous_module(name: str, rpc_name: str, marker: str) -> dict:
    registry = HandlerRegistry()

    def handler(rid, params):
        return {"marker": marker, "rid": rid, "params": params}

    handler.__name__ = "_"
    handler.__module__ = name
    registry.method(rpc_name)(handler)
    return {
        "__name__": name,
        "_registry": registry,
        "method": registry.method,
        "_": handler,
    }


def _named_helper_module(name: str, marker: str) -> dict:
    def helper():
        return marker

    helper.__module__ = name
    return {"__name__": name, "helper": helper}


def test_anonymous_registered_handlers_bind_without_helper_collision():
    server = SimpleNamespace(_methods={})
    server.register_method = lambda name, fn: server._methods.__setitem__(name, fn)

    bind_module(_anonymous_module("synthetic.subagents", "subagent.list", "subagents"), server)
    bind_module(_anonymous_module("synthetic.profiles", "profiles.list", "profiles"), server)

    assert not hasattr(server, "_")
    assert server._methods["subagent.list"]("one", {"x": 1}) == {
        "marker": "subagents", "rid": "one", "params": {"x": 1},
    }
    assert server._methods["profiles.list"]("two", {"y": 2}) == {
        "marker": "profiles", "rid": "two", "params": {"y": 2},
    }


def test_named_helper_collision_raises_without_replacing_first_helper():
    server = SimpleNamespace(_methods={})
    bind_module(_named_helper_module("synthetic.first", "first"), server)
    first_helper = server.helper

    with pytest.raises(RuntimeError, match="split-module name collision"):
        bind_module(_named_helper_module("synthetic.second", "second"), server)

    assert server.helper is first_helper
    assert server.helper() == "first"
