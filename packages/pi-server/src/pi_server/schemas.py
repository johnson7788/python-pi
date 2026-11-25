"""Request bodies, one class per endpoint family.

They describe the wire, not the behaviour: a route module that needs ``LoginRequest``
should not have to import the application factory.
"""

from __future__ import annotations
from typing import Any
from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    displayName: str = ""


class PasswordChangeRequest(BaseModel):
    currentPassword: str
    newPassword: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    displayName: str = ""
    role: str = "user"
    mustChangePassword: bool = True
    monthlyTokenQuota: int | None = None
    monthlyCostQuota: float | None = None


class UpdateUserRequest(BaseModel):
    """Every field is optional; only what the client sent is applied."""

    displayName: str | None = None
    role: str | None = None
    status: str | None = None
    password: str | None = None
    mustChangePassword: bool | None = None
    monthlyTokenQuota: int | None = None
    monthlyCostQuota: float | None = None


class WriteFileRequest(BaseModel):
    """The JSON half of ``PUT /api/workspace/file``; raw bytes are the other half."""

    content: str


class SkillToggleRequest(BaseModel):
    enabled: bool


class CreateWorkspaceRequest(BaseModel):
    name: str


class PluginToggleRequest(BaseModel):
    enabled: bool
    appId: str | None = None


class CreateScheduleRequest(BaseModel):
    name: str
    prompt: str
    kind: str
    workspace: str = "default"
    appId: str = "general"
    modeId: str = "office"
    runAt: str | None = None
    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None
    intervalMinutes: int | None = None


class UpdateScheduleRequest(BaseModel):
    enabled: bool


class MarketRatingRequest(BaseModel):
    score: int


class FeedbackRequest(BaseModel):
    sessionFile: str
    timestamp: float
    rating: int
    comment: str = ""


class ExportDocxRequest(BaseModel):
    markdown: str
    title: str = ""
    subtitle: str = ""


class AppUpsertRequest(BaseModel):
    name: str
    description: str = ""
    accent: str = "#3b5fba"
    modes: list[dict[str, Any]] = []


class RenameWorkspaceRequest(BaseModel):
    name: str


class CreateDirectoryRequest(BaseModel):
    parentPath: str = "/"
    name: str

