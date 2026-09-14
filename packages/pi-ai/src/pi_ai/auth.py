"""Credential storage and API-key resolution.

This module is the Python counterpart of pi's ``src/auth/*`` plus the
coding-agent's ``auth-storage.ts`` / ``runtime-credentials.ts``.  It is split
into three layers on purpose, mirroring upstream:

* :class:`ApiKeyCredential` / :class:`OAuthCredential` - the **wire format** of
  ``~/.pi/agent/auth.json``.  The file is shared with the TypeScript ``pi``, so
  the JSON shapes (``type``, ``key``, ``refresh``/``access``/``expires`` and
  provider-specific extra keys) are frozen even though the Python API is
  snake_case.
* :class:`CredentialStore` - persistence, one credential per provider id.
  :class:`FileCredentialStore` writes atomically with mode ``0o600``;
  :class:`InMemoryCredentialStore` exists for tests and ephemeral processes.
* :class:`AuthStorage` - the facade the rest of pi-ai consumes.  It layers
  non-persistent runtime overrides over the store and knows the environment
  variable map.

Resolution order (highest first) is the upstream one and must stay that way::

    runtime override -> stored credential -> environment variable

The critical invariant is "a stored credential owns the provider": ambient
environment variables are only consulted when the store has no credential for
the provider (or when the stored api-key credential holds no key of its own,
which is exactly how upstream's ``envApiKeyAuth`` behaves).  In particular a
stored OAuth credential never silently degrades to an environment API key.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Union, runtime_checkable

from .errors import PiAiError

__all__ = [
    "ENV_API_KEYS",
    "ApiKeyCredential",
    "AuthStorage",
    "AuthStorageError",
    "Credential",
    "CredentialInfo",
    "CredentialStore",
    "EnvSource",
    "FileCredentialStore",
    "InMemoryCredentialStore",
    "OAuthCredential",
    "ResolvedAuth",
    "credential_from_json",
    "credential_to_json",
]


class AuthStorageError(PiAiError):
    """A credential file is unreadable or does not contain valid credentials."""


# --------------------------------------------------------------------------------------
# Credentials (the auth.json wire format)
# --------------------------------------------------------------------------------------


@dataclass
class ApiKeyCredential:
    """A stored api key.

    ``env`` carries provider-scoped *configuration* values (Cloudflare account
    ids, Azure resource names, ...), not secrets: upstream keeps them on the
    credential so that resolution can read them without another config source.
    """

    key: str | None = None
    env: dict[str, str] | None = None
    type: Literal["api_key"] = field(default="api_key", init=False)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "api_key"}
        if self.key is not None:
            data["key"] = self.key
        if self.env is not None:
            data["env"] = dict(self.env)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ApiKeyCredential:
        kind = data.get("type", "api_key")
        if kind != "api_key":
            raise AuthStorageError(f"Expected an api_key credential, got type {kind!r}")
        key = data.get("key")
        if key is not None and not isinstance(key, str):
            raise AuthStorageError("Credential 'key' must be a string")
        env = data.get("env")
        if env is not None:
            if not isinstance(env, Mapping) or not all(isinstance(v, str) for v in env.values()):
                raise AuthStorageError("Credential 'env' must be a mapping of strings")
            env = {str(k): str(v) for k, v in env.items()}
        return cls(key=key, env=env)


@dataclass
class OAuthCredential:
    """A stored OAuth token.

    ``expires`` is Unix **milliseconds** (upstream uses ``Date.now()``).
    Providers persist extra fields next to the canonical ones; they are kept in
    :attr:`extra` and written back at the top level so a round-trip through
    Python does not drop provider state.
    """

    refresh: str
    access: str
    expires: int
    extra: dict[str, Any] = field(default_factory=dict)
    type: Literal["oauth"] = field(default="oauth", init=False)

    RESERVED_KEYS = ("type", "refresh", "access", "expires")

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "oauth"}
        for key, value in self.extra.items():
            if key not in self.RESERVED_KEYS:
                data[key] = value
        data["refresh"] = self.refresh
        data["access"] = self.access
        data["expires"] = self.expires
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> OAuthCredential:
        if data.get("type") != "oauth":
            raise AuthStorageError(f"Expected an oauth credential, got type {data.get('type')!r}")
        refresh = data.get("refresh")
        access = data.get("access")
        expires = data.get("expires")
        if not isinstance(refresh, str) or not isinstance(access, str):
            raise AuthStorageError("OAuth credential requires string 'refresh' and 'access'")
        # ``bool`` is an ``int`` subclass; a boolean timestamp is a bug, not a value.
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            raise AuthStorageError("OAuth credential requires a numeric 'expires'")
        extra = {k: v for k, v in data.items() if k not in cls.RESERVED_KEYS}
        return cls(refresh=refresh, access=access, expires=int(expires), extra=extra)

    def is_expired(self, *, now_ms: int | None = None, minimum_validity_ms: int = 0) -> bool:
        """Whether the access token has less than ``minimum_validity_ms`` left.

        Upstream treats a token with under five minutes of validity as expired so
        a request cannot start with a token that dies mid-flight.
        """
        if now_ms is None:
            import time

            now_ms = int(time.time() * 1000)
        return now_ms + max(0, minimum_validity_ms) >= self.expires


Credential = Union[ApiKeyCredential, OAuthCredential]


def credential_to_json(credential: Credential) -> dict[str, Any]:
    """Serialise a credential to its ``auth.json`` object form."""
    if isinstance(credential, (ApiKeyCredential, OAuthCredential)):
        return credential.to_json()
    raise AuthStorageError(f"Unknown credential type: {credential!r}")


def credential_from_json(data: Mapping[str, Any]) -> Credential:
    """Parse one ``auth.json`` entry, dispatching on its ``type`` tag."""
    if not isinstance(data, Mapping):
        raise AuthStorageError("Credential entry must be an object")
    kind = data.get("type")
    if kind == "api_key":
        return ApiKeyCredential.from_json(data)
    if kind == "oauth":
        return OAuthCredential.from_json(data)
    raise AuthStorageError(f"Unknown credential type: {kind!r}")


@dataclass(frozen=True)
class CredentialInfo:
    """Non-secret credential metadata for account/status enumeration."""

    provider_id: str
    type: Literal["api_key", "oauth"]


# --------------------------------------------------------------------------------------
# Credential stores
# --------------------------------------------------------------------------------------


@runtime_checkable
class CredentialStore(Protocol):
    """App-owned storage, keyed by ``Provider.id`` (one credential per provider)."""

    def get(self, provider_id: str) -> Credential | None:
        """Read the stored credential (possibly expired), or ``None``."""
        ...

    def set(self, provider_id: str, credential: Credential) -> None:
        """Create or replace the credential for ``provider_id``."""
        ...

    def delete(self, provider_id: str) -> None:
        """Remove the credential for ``provider_id``; missing is a no-op."""
        ...

    def list(self) -> list[CredentialInfo]:
        """List metadata for every stored credential without exposing secrets."""
        ...


class InMemoryCredentialStore:
    """A process-local credential store.

    Values are deep-copied on the way in and out so callers cannot mutate the
    store's state through a reference they still hold - the same protection
    upstream gets from ``structuredClone``/spread copies.
    """

    def __init__(self, credentials: Mapping[str, Credential] | None = None) -> None:
        self._credentials: dict[str, Credential] = {}
        for provider_id, credential in (credentials or {}).items():
            self._credentials[provider_id] = copy.deepcopy(credential)

    def get(self, provider_id: str) -> Credential | None:
        credential = self._credentials.get(provider_id)
        return copy.deepcopy(credential) if credential is not None else None

    def set(self, provider_id: str, credential: Credential) -> None:
        self._credentials[provider_id] = copy.deepcopy(credential)

    def delete(self, provider_id: str) -> None:
        self._credentials.pop(provider_id, None)

    def list(self) -> list[CredentialInfo]:
        return [
            CredentialInfo(provider_id=provider_id, type=credential.type)
            for provider_id, credential in self._credentials.items()
        ]


class FileCredentialStore:
    """``auth.json``-backed credential store.

    The file is a single JSON object ``{providerId: Credential}`` (no wrapping
    key), exactly like the TypeScript implementation, so an existing
    ``~/.pi/agent/auth.json`` can be read and written interchangeably.

    Writes are atomic: the document is rendered into a temporary file in the
    same directory and then ``os.replace``-d over the target, so readers never
    observe a half-written file.  The file mode is set to ``0o600`` when it is
    created; an existing file keeps whatever mode it already had, because an
    administrator may have tightened or loosened it deliberately (upstream does
    the same by passing ``mode`` only on creation).
    """

    #: Mode for a newly created auth.json (owner read/write only).
    FILE_MODE = 0o600
    #: Mode for a newly created parent directory.
    DIR_MODE = 0o700

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    # -- CredentialStore -------------------------------------------------------------

    def get(self, provider_id: str) -> Credential | None:
        credential = self._read().get(provider_id)
        return copy.deepcopy(credential) if credential is not None else None

    def set(self, provider_id: str, credential: Credential) -> None:
        data = self._read()
        data[provider_id] = copy.deepcopy(credential)
        self._write(data)

    def delete(self, provider_id: str) -> None:
        data = self._read()
        if provider_id not in data:
            return
        del data[provider_id]
        self._write(data)

    def list(self) -> list[CredentialInfo]:
        return [
            CredentialInfo(provider_id=provider_id, type=credential.type)
            for provider_id, credential in self._read().items()
        ]

    # -- internals -------------------------------------------------------------------

    def _read(self) -> dict[str, Credential]:
        try:
            text = self._path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise AuthStorageError(f"Failed to read auth.json: {error}") from error
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as error:
            raise AuthStorageError(f"Invalid auth.json: {error}") from error
        if not isinstance(parsed, dict):
            raise AuthStorageError("Invalid auth.json: expected an object")
        credentials: dict[str, Credential] = {}
        for provider_id, entry in parsed.items():
            try:
                credentials[str(provider_id)] = credential_from_json(entry)
            except AuthStorageError as error:
                raise AuthStorageError(
                    f"Invalid auth.json credential for provider {provider_id!r}: {error}"
                ) from error
        return credentials

    def _write(self, data: Mapping[str, Credential]) -> None:
        parent = self._path.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, self.DIR_MODE)
            except OSError:
                pass  # Windows and some filesystems do not support POSIX modes.
        try:
            mode = stat.S_IMODE(self._path.stat().st_mode)
        except FileNotFoundError:
            mode = self.FILE_MODE
        payload = json.dumps(
            {provider_id: credential_to_json(credential) for provider_id, credential in data.items()},
            indent=2,
        )
        handle, temp_name = tempfile.mkstemp(dir=str(parent), prefix=".auth.json.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload)
                file.write("\n")
            os.chmod(temp_name, mode)
            os.replace(temp_name, self._path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------------------------
# Environment variables
# --------------------------------------------------------------------------------------

#: Provider id -> environment variables, in priority order (first non-empty wins).
#: Only providers with a plain API-key variable are listed; anthropic's
#: ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_OAUTH_TOKEN`` are deliberately absent
#: because they are not api keys (upstream ``getEnvApiKey`` skips them too).
ENV_API_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
}

#: Either a plain mapping of environment variables or a lookup function.
EnvSource = Union[Mapping[str, str], Callable[[str], "str | None"]]


def _os_env(name: str) -> str | None:
    """Default environment reader: blank values count as unset (upstream rule)."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class ResolvedAuth:
    """The outcome of :meth:`AuthStorage.resolve`."""

    provider: str
    api_key: str | None
    source: str
    credential: Credential | None = None


class AuthStorage:
    """Facade that layers runtime API keys over a :class:`CredentialStore`.

    ``store`` defaults to an empty :class:`InMemoryCredentialStore` and ``env``
    defaults to ``os.environ`` (blank values treated as unset).  Pass an explicit
    mapping/function for hermetic tests.
    """

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        env: EnvSource | None = None,
    ) -> None:
        self._store: CredentialStore = store if store is not None else InMemoryCredentialStore()
        self._env: EnvSource = env if env is not None else _os_env
        self._runtime_api_keys: dict[str, str] = {}

    @property
    def store(self) -> CredentialStore:
        return self._store

    # -- runtime overrides -----------------------------------------------------------

    def set_runtime_api_key(self, provider: str, api_key: str) -> None:
        """Register a non-persistent key (e.g. CLI ``--api-key`` for this process)."""
        self._runtime_api_keys[provider] = api_key

    def remove_runtime_api_key(self, provider: str) -> None:
        self._runtime_api_keys.pop(provider, None)

    def has_runtime_api_key(self, provider: str) -> bool:
        return provider in self._runtime_api_keys

    # -- stored credentials ----------------------------------------------------------

    def get_credential(self, provider: str) -> Credential | None:
        """Return the effective credential: runtime override first, then the store."""
        override = self._runtime_api_keys.get(provider)
        if override is not None:
            return ApiKeyCredential(key=override)
        return self._store.get(provider)

    def set_credential(self, provider: str, credential: Credential) -> None:
        self._store.set(provider, credential)

    def set_api_key(self, provider: str, api_key: str) -> None:
        self._store.set(provider, ApiKeyCredential(key=api_key))

    def delete_credential(self, provider: str) -> None:
        """Log out: drop both the stored credential and any runtime override."""
        self._store.delete(provider)
        self._runtime_api_keys.pop(provider, None)

    def list_credentials(self) -> list[CredentialInfo]:
        """Metadata for stored credentials plus runtime overrides, de-duplicated."""
        entries: dict[str, CredentialInfo] = {
            info.provider_id: info for info in self._store.list()
        }
        for provider in self._runtime_api_keys:
            entries[provider] = CredentialInfo(provider_id=provider, type="api_key")
        return list(entries.values())

    def list_configured_providers(self) -> list[str]:
        """Provider ids with configured auth (used to filter available models)."""
        return sorted({info.provider_id for info in self.list_credentials()})

    # -- resolution ------------------------------------------------------------------

    def has_auth(self, provider: str) -> bool:
        """Whether the provider has *any* usable auth configuration."""
        return self.resolve(provider) is not None

    def resolve(self, provider: str) -> ResolvedAuth | None:
        """Resolve request auth for ``provider``.

        Order: runtime override -> stored credential -> environment variable.
        A stored credential owns the provider: when the store holds one, the
        environment is not consulted unless the credential is an api-key
        credential with no key at all (upstream ``envApiKeyAuth`` semantics).
        """
        override = self._runtime_api_keys.get(provider)
        if override is not None:
            return ResolvedAuth(provider=provider, api_key=override, source="runtime")

        credential = self._store.get(provider)
        if credential is not None:
            if isinstance(credential, OAuthCredential):
                # OAuth refresh is out of scope for v0.1: report the stored access
                # token only while it is still valid, and never fall back to env.
                api_key = credential.access if not credential.is_expired() else None
                return ResolvedAuth(
                    provider=provider, api_key=api_key, source="OAuth", credential=credential
                )
            if credential.key:
                return ResolvedAuth(
                    provider=provider,
                    api_key=credential.key,
                    source="stored credential",
                    credential=credential,
                )
            # Key-less api_key credential: nothing stored can shadow the ambient.
            env_name, value = self._find_env(provider)
            if value is not None:
                return ResolvedAuth(
                    provider=provider, api_key=value, source=env_name, credential=credential
                )
            return None

        env_name, value = self._find_env(provider)
        if value is not None:
            return ResolvedAuth(provider=provider, api_key=value, source=env_name)
        return None

    def resolve_api_key(self, provider: str) -> str | None:
        """The api key for ``provider``, or ``None`` when none is configured."""
        resolved = self.resolve(provider)
        return resolved.api_key if resolved is not None else None

    # -- internals -------------------------------------------------------------------

    def _find_env(self, provider: str) -> tuple[str, str | None]:
        for name in ENV_API_KEYS.get(provider, ()):
            value = self._read_env(name)
            if value is not None:
                return name, value
        return "", None

    def _read_env(self, name: str) -> str | None:
        if callable(self._env):
            value = self._env(name)
        else:
            value = self._env.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None
