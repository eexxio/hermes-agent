"""Profile-scoped device registrations and best-effort detached reply delivery."""
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

from agent.memory_provider import spawn_context_thread
from agent.runtime_scope import bind_profile_scope
from gateway.owner_identity import host_owner_subject, same_owner
from hermes_cli.apns import APNsConfig, APNsSender
from hermes_cli.config_effective import load_user_config_effective
from hermes_constants import mkdir_under_hermes_home

logger = logging.getLogger(__name__)


class DeviceStore:
    def __init__(self, home):
        self.home = Path(home)

    def _connect(self):
        mkdir_under_hermes_home(self.home)
        db = sqlite3.connect(self.home / "push_devices.db", timeout=5)
        db.execute("CREATE TABLE IF NOT EXISTS devices (owner TEXT, token TEXT, bundle_id TEXT NOT NULL, "
                   "PRIMARY KEY (owner, token))")
        return db

    def register(self, owner, token, bundle_id):
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO devices VALUES (?, ?, ?) ON CONFLICT(owner, token) DO UPDATE "
                       "SET bundle_id=excluded.bundle_id", (owner, token.lower(), bundle_id))

    def unregister(self, owner, token):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM devices WHERE owner=? AND token=?", (owner, token.lower()))

    def devices(self, owner):
        with closing(self._connect()) as db:
            return db.execute("SELECT token, bundle_id FROM devices WHERE owner=?", (owner,)).fetchall()


def load_apns_config():
    from gateway.platforms._shared import get_scoped_secret
    section = (load_user_config_effective().get("push") or {}).get("apns") or {}
    team = section.get("team_id") or get_scoped_secret("APNS_TEAM_ID")
    key_id = section.get("key_id") or get_scoped_secret("APNS_KEY_ID")
    path = section.get("key_path") or get_scoped_secret("APNS_KEY_PATH")
    key = get_scoped_secret("APNS_AUTH_KEY")
    topic = section.get("bundle_id")
    environment = section.get("environment", "production")
    if not all((team, key_id, topic, key or path)) or environment not in {"production", "sandbox"}:
        return None
    if not key:
        key_path = Path(path).expanduser()
        if not key_path.is_absolute():
            from hermes_constants import get_hermes_home
            key_path = get_hermes_home() / key_path
        try:
            key = key_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
    return APNsConfig(team, key_id, key, topic, environment)


_senders = {}
_senders_lock = threading.Lock()


def reply_payload(session_id, title, payload):
    title = title or "Hermes"
    preview = str(payload.get("text") or payload.get("content") or payload.get("error") or "")[:200]
    return {"aps": {"alert": {"title": title, "body": preview}, "sound": "default"},
            "session_id": session_id, "title": title, "preview": preview, "status": payload["status"]}


def _deliver(home, owner, session_id, title, payload):
    try:
        with bind_profile_scope(home):
            config = load_apns_config()
            if config is None:
                return
            with _senders_lock:
                sender = _senders.get((str(home), config))
                if sender is None:
                    sender = _senders[(str(home), config)] = APNsSender(config)
            store = DeviceStore(home)
            if not title and (Path(home) / "state.db").exists():
                from persistence_assembly import open_persistence_reader
                with open_persistence_reader(Path(home)) as reader:
                    record = reader.sessions.get(payload.get("_title_session_id") or session_id)
                    title = record.title if record else None
            for token, bundle in store.devices(owner):
                # A provider key may serve several apps, but registration cannot choose an arbitrary topic.
                if bundle != config.topic:
                    continue
                try:
                    response = sender.send(token, reply_payload(session_id, title, payload))
                    if response.status_code in {400, 410}:
                        reason = response.json().get("reason")
                        if reason in {"BadDeviceToken", "Unregistered"}:
                            store.unregister(owner, token)
                except Exception:
                    logger.warning("APNs reply delivery failed", exc_info=True)
    except Exception:
        logger.warning("APNs reply notification failed", exc_info=True)


def notify_reply(home, owner, session_id, title, payload, *, attached):
    """Capture the settlement snapshot; do all config, storage and network I/O off-thread."""
    if attached or payload.get("status") not in {"complete", "error"}:
        return
    try:
        subject = host_owner_subject()
        if not owner or not same_owner(owner, subject):
            return
        spawn_context_thread(_deliver, name="apns-reply", args=(Path(home), subject, session_id, title,
                                                              dict(payload))).start()
    except Exception:
        logger.warning("Could not schedule APNs reply notification", exc_info=True)


def notify_standalone_reply(sid, session, payload):
    from hermes_constants import get_hermes_home
    from tui_gateway import server
    try:
        with server._session_transport_lock:
            attached = server._session_has_live_transport(session) or server._stdio_is_rpc_channel
        owner = server._session_auth_user_id(session) or host_owner_subject()
        notify_reply(session.get("profile_home") or get_hermes_home(), owner, sid,
                     session.get("pending_title"),
                     {**payload, "_title_session_id": server._session_lookup_key(session, fallback=sid)},
                     attached=attached)
    except Exception:
        logger.warning("Could not schedule standalone reply notification", exc_info=True)
