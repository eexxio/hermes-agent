"""Both standalone turn terminal paths schedule only detached-owner notifications."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.owner_identity import host_owner_subject
from hermes_cli import push_notifications as push
from tests.tui_gateway import test_turn_finished_failure_cause as turn_fixtures
from tui_gateway import server
from tui_gateway.transport import FanoutTransport

turn_env = turn_fixtures.turn_env


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "returned_error", "exception"])
def test_turn_terminal_push(turn_env, monkeypatch, attached, outcome):
    def run(*args, **kwargs):
        if outcome == "exception":
            raise RuntimeError("provider unavailable")
        if outcome == "returned_error":
            return {"final_response": "", "error": "provider unavailable", "failed": True}
        return {"final_response": "finished"}

    session = turn_fixtures._session(agent=SimpleNamespace(session_id="stored", run_conversation=run,
                                            clear_interrupt=lambda: None),
                       auth_user_id=host_owner_subject(),
                       transport=FanoutTransport(*([Mock()] if attached else [])))
    spawn = Mock()
    monkeypatch.setattr(push, "spawn_context_thread", spawn)
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    server._run_prompt_submit("request", "ui-session", session, "hello")
    if attached:
        spawn.assert_not_called()
    else:
        spawn.assert_called_once()
        payload = spawn.call_args.kwargs["args"][-1]
        assert payload["status"] == ("complete" if outcome == "complete" else "error")
        assert (payload.get("text") or payload.get("error"))
