"""APNs registration, provider wire contract, and detached reply delivery."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from gateway.owner_identity import host_owner_subject
from hermes_cli.apns import APNsConfig, APNsSender
from hermes_cli import push_notifications as push
from hermes_constants import get_hermes_home


@pytest.fixture
def signing_key():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, APNsConfig("team", "key-id", pem, "com.example.orbit", "sandbox")


@pytest.fixture
def dashboard(monkeypatch):
    from hermes_cli import web_server
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    yield client
    client.close()


def test_registration_upsert_unregister_auth_and_validation(dashboard):
    token = "AB" * 32
    body = {"token": token, "platform": "ios", "bundle_id": "com.example.orbit"}
    assert dashboard.post("/api/push/register", json=body).status_code == 200
    body["bundle_id"] = "com.example.new"
    assert dashboard.post("/api/push/register", json=body).status_code == 200
    store = push.DeviceStore(get_hermes_home())
    assert store.devices(host_owner_subject()) == [(token.lower(), "com.example.new")]
    for changes in ({"platform": "android"}, {"token": "not-hex"}, {"token": ""},
                    {"bundle_id": ""}, {"bundle_id": " "}):
        assert dashboard.post("/api/push/register", json={**body, **changes}).status_code == 422
    for _ in range(2):
        assert dashboard.post("/api/push/unregister", json={"token": token}).status_code == 200
    assert store.devices(host_owner_subject()) == []
    dashboard.headers.clear()
    assert dashboard.post("/api/push/register", json=body).status_code == 401
    assert dashboard.post("/api/push/unregister", json={"token": token}).status_code == 401


def test_device_store_isolates_profiles_and_owners(tmp_path):
    first, second = push.DeviceStore(tmp_path / "first"), push.DeviceStore(tmp_path / "second")
    first.register("owner", "ab", "app")
    first.register("other", "cd", "app")
    assert second.devices("owner") == []
    first.unregister("other", "ab")
    assert first.devices("owner") == [("ab", "app")]


def test_sender_headers_payload_and_cached_verifiable_jwt(signing_key):
    key, config = signing_key
    requests = []
    sender = APNsSender(config, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200)))
    payload = push.reply_payload("session", "Title", {"status": "complete", "text": "answer" * 60})
    try:
        sender.send("ab", payload)
        sender.send("ab", {"event": "update"}, push_type="liveactivity", topic="app.push-type.liveactivity",
                    priority=5, expiration=42, collapse_id="turn")
    finally:
        sender.close()
    first, second = requests
    assert first.url.host == "api.sandbox.push.apple.com"
    assert first.headers["apns-push-type"] == "alert"
    assert first.headers["apns-topic"] == config.topic
    authorization = first.headers["authorization"]
    assert authorization.startswith("bearer ")
    token = authorization.split()[1]
    assert jwt.decode(token, key.public_key(), algorithms=["ES256"])["iss"] == config.team_id
    assert jwt.get_unverified_header(token)["kid"] == config.key_id
    assert authorization == second.headers["authorization"]
    assert second.headers["apns-push-type"] == "liveactivity"
    assert second.headers["apns-topic"] == "app.push-type.liveactivity"
    assert second.headers["apns-priority"] == "5"
    assert second.headers["apns-expiration"] == "42"
    assert second.headers["apns-collapse-id"] == "turn"
    sent = json.loads(first.content)
    assert sent["aps"] == {"alert": {"title": sent["title"], "body": sent["preview"]}, "sound": "default"}
    assert sent["session_id"] == "session" and sent["status"] == "complete"
    assert len(sent["preview"]) == 200


def test_jwt_refresh_and_production(signing_key, monkeypatch):
    _, config = signing_key
    config = APNsConfig(config.team_id, config.key_id, config.auth_key, config.topic)
    requests = []
    sender = APNsSender(config, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200)))
    monkeypatch.setattr("hermes_cli.apns.time.time", lambda: 10000)
    sender.send("ab", {})
    monkeypatch.setattr("hermes_cli.apns.time.time", lambda: 13001)
    sender.send("ab", {})
    sender.close()
    assert requests[0].url.host == "api.push.apple.com"
    assert requests[0].headers["authorization"] != requests[1].headers["authorization"]


@pytest.mark.parametrize("code,reason,removed", [(410, "Unregistered", True),
                                                 (400, "BadDeviceToken", True),
                                                 (400, "BadTopic", False), (500, "InternalServerError", False)])
def test_delivery_removes_only_invalid_tokens(signing_key, monkeypatch, code, reason, removed):
    _, config = signing_key
    from hermes_cli.config import atomic_config_write
    atomic_config_write(get_hermes_home() / "config.yaml", {
        "push": {"apns": {"team_id": config.team_id, "key_id": config.key_id,
                           "bundle_id": config.topic, "key_path": "key.p8", "environment": "sandbox"}}})
    (get_hermes_home() / "key.p8").write_text(config.auth_key, encoding="utf-8")
    requests = []
    sender = APNsSender(config, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(code, json={"reason": reason})))
    monkeypatch.setattr(push, "_senders", {})
    monkeypatch.setattr(push, "APNsSender", lambda cfg: sender)
    store, owner = push.DeviceStore(get_hermes_home()), host_owner_subject()
    store.register(owner, "ab", config.topic)
    store.register(owner, "cd", "other.app")
    push._deliver(get_hermes_home(), owner, "sid", "", {"status": "error", "error": "failed"})
    sender.close()
    assert len(requests) == 1
    assert json.loads(requests[0].content)["preview"] == "failed"
    assert json.loads(requests[0].content)["title"] == "Hermes"
    assert (("ab", config.topic) not in store.devices(owner)) is removed
    assert ("cd", "other.app") in store.devices(owner)


def test_unconfigured_is_off_and_payload_empty(monkeypatch):
    monkeypatch.setattr(push, "APNsSender", Mock(side_effect=AssertionError("must stay off")))
    push._deliver(get_hermes_home(), host_owner_subject(), "sid", None, {"status": "complete", "text": ""})
    push.APNsSender.assert_not_called()
    assert push.reply_payload("sid", None, {"status": "complete"})["preview"] == ""


def test_schedule_filters_viewers_status_owner_and_never_raises(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(push, "spawn_context_thread", spawn)
    owner = host_owner_subject()
    for attached, status, subject in [(True, "complete", owner), (False, "interrupted", owner),
                                      (False, "complete", "foreign")]:
        push.notify_reply(get_hermes_home(), subject, "sid", "title", {"status": status}, attached=attached)
    spawn.assert_not_called()
    for status in ("complete", "error"):
        push.notify_reply(get_hermes_home(), owner, "sid", "title", {"status": status}, attached=False)
    assert spawn.call_count == 2
    spawn.side_effect = RuntimeError("cannot start")
    push.notify_reply(get_hermes_home(), owner, "sid", "title", {"status": "complete"}, attached=False)


@pytest.mark.parametrize("attached", [False, True])
def test_standalone_live_membership_not_replay(monkeypatch, attached):
    from tui_gateway import server
    from tui_gateway.transport import FanoutTransport
    transport = FanoutTransport(*([Mock()] if attached else []))
    session = {"transport": transport, "profile_home": str(get_hermes_home()), "auth_user_id": host_owner_subject()}
    monkeypatch.setattr(server, "_stdio_is_rpc_channel", False)
    notify = Mock()
    monkeypatch.setattr(push, "notify_reply", notify)
    push.notify_standalone_reply("sid", session, {"status": "complete", "text": "done"})
    assert notify.call_args.kwargs["attached"] is attached


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("outcome,response,captured,status", [
    ("completed", "done", {}, "complete"),
    ("failed", None, {"result": {"error": "provider unavailable"}}, "error")])
def test_gateway_completion_uses_subscribers(monkeypatch, attached, outcome, response, captured, status):
    from gateway.session_events import SessionEvents
    from gateway.session_settlement_recovery import _publish_completion
    live = SimpleNamespace(controls=Mock(), source=SimpleNamespace(user_id=host_owner_subject()),
                           event_stream=SessionEvents(), subscribers={"view": object()} if attached else {})
    notify = Mock()
    monkeypatch.setattr(push, "notify_reply", notify)
    with live.event_stream.lock:
        _publish_completion(SimpleNamespace(profile_id=str(get_hermes_home())), live,
                            SimpleNamespace(session_id="sid"), {"admission_id": "a", "request_id": "r"},
                            {"outcome": outcome}, response, captured)
    assert notify.call_args.kwargs["attached"] is attached
    payload = notify.call_args.args[4]
    assert payload["status"] == status
    assert push.reply_payload("sid", None, payload)["preview"] == (response or "provider unavailable")
