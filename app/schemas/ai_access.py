"""HTTP response shapes for administrative AI client-key access."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class AiClientKeyView(BaseModel):
    client_id: str
    status: str
    created_at: dt.datetime
    revoked_at: dt.datetime | None


class IssuedAiClientKeyView(BaseModel):
    client_id: str
    token: str
    status: str
    created_at: dt.datetime
