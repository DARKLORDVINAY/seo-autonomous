"""Separate machine, reviewer and administrator capabilities; no authority in request JSON."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.app.config.settings import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    role: str
    actor: str


def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    if not any((settings.api_token, settings.approval_token, settings.admin_token)):
        raise HTTPException(503, "Control plane is locked. Configure distinct API, approval and administrator tokens.")
    token = credentials.credentials if credentials else ""
    for role, secret in (("operator", settings.api_token), ("reviewer", settings.approval_token), ("admin", settings.admin_token)):
        if secret and token and hmac.compare_digest(token, secret.get_secret_value()):
            return Principal(role, {"operator": "control-operator", "reviewer": "human-reviewer", "admin": "site-administrator"}[role])
    raise HTTPException(401, "Valid bearer capability required", headers={"WWW-Authenticate": "Bearer"})


def reviewer(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    if principal.role not in {"reviewer", "admin"}:
        raise HTTPException(403, "An independent human review capability is required")
    return principal


def administrator(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    if principal.role != "admin":
        raise HTTPException(403, "Administrator capability required")
    return principal
