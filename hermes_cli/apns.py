"""Reusable HTTP/2 APNs provider client."""
from dataclasses import dataclass
import threading
import time

import httpx
import jwt


@dataclass(frozen=True)
class APNsConfig:
    team_id: str
    key_id: str
    auth_key: str
    topic: str
    environment: str = "production"


class APNsSender:
    def __init__(self, config: APNsConfig, *, transport=None):
        self.config = config
        self._client = httpx.Client(http2=True, transport=transport, timeout=10)
        self._lock = threading.Lock()
        self._jwt = ""
        self._issued = 0

    def _authorization(self):
        with self._lock:
            now = int(time.time())
            if not self._jwt or not 0 <= now - self._issued < 3000:
                self._jwt = jwt.encode({"iss": self.config.team_id, "iat": now}, self.config.auth_key,
                                       algorithm="ES256", headers={"kid": self.config.key_id})
                self._issued = now
            return "bearer " + self._jwt

    def send(self, device_token, payload, *, push_type="alert", topic=None, priority=10,
             expiration=0, collapse_id=None):
        headers = {"authorization": self._authorization(), "apns-push-type": push_type,
                   "apns-topic": topic or self.config.topic, "apns-priority": str(priority),
                   "apns-expiration": str(expiration)}
        if collapse_id is not None:
            headers["apns-collapse-id"] = collapse_id
        host = "api.sandbox.push.apple.com" if self.config.environment == "sandbox" else "api.push.apple.com"
        return self._client.post(f"https://{host}/3/device/{device_token}", json=payload, headers=headers)

    def close(self):
        self._client.close()
