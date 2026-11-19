"""Authenticated iOS device registration for the dashboard owner."""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from gateway.owner_identity import host_owner_subject
from hermes_cli.push_notifications import DeviceStore
from hermes_cli.web_server_profiles import _config_profile_scope
from hermes_constants import get_hermes_home

router = APIRouter()


class DeviceToken(BaseModel):
    token: str = Field(min_length=2, max_length=512, pattern=r"^(?:[0-9a-fA-F]{2})+$")


class RegisterDevice(DeviceToken):
    platform: Literal["ios"]
    bundle_id: str = Field(min_length=1, max_length=255, pattern=r"^\S+$")


@router.post("/api/push/register")
def register_device(body: RegisterDevice, profile: str | None = None):
    with _config_profile_scope(profile):
        DeviceStore(get_hermes_home()).register(host_owner_subject(), body.token, body.bundle_id)
    return {"ok": True}


@router.post("/api/push/unregister")
def unregister_device(body: DeviceToken, profile: str | None = None):
    with _config_profile_scope(profile):
        DeviceStore(get_hermes_home()).unregister(host_owner_subject(), body.token)
    return {"ok": True}
