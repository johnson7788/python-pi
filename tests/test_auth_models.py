"""Tests for credential storage (``pi_ai.auth``) and the catalog (``pi_ai.models``)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from pi_ai.auth import (
    ApiKeyCredential,
    AuthStorage,
    AuthStorageError,
    FileCredentialStore,
    InMemoryCredentialStore,
    OAuthCredential,
    credential_from_json,
)
from pi_ai.models import (
    BUILTIN_CATALOG,
    ModelRegistry,
    ModelsConfigError,
    get_model,
    load_models_json,
)

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _missing_models_path(tmp_path: Path) -> Path:
    """A path that does not exist, so only the builtin pack is loaded."""
    return tmp_path / "no-such-models.json"


def _registry(tmp_path: Path, *, auth: AuthStorage | None = None) -> ModelRegistry:
    return ModelRegistry(auth=auth or AuthStorage(env={}), models_path=_missing_models_path(tmp_path))


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# Credential wire format
# --------------------------------------------------------------------------------------


def test_api_key_credential_json_round_trip() -> None:
    credential = ApiKeyCredential(key="sk-test", env={"ACCOUNT_ID": "abc"})
    data = credential.to_json()
    assert data == {"type": "api_key", "key": "sk-test", "env": {"ACCOUNT_ID": "abc"}}
    assert credential_from_json(data) == credential


def test_api_key_credential_drops_absent_fields() -> None:
    assert ApiKeyCredential().to_json() == {"type": "api_key"}


def test_oauth_credential_json_round_trip_keeps_extra_fields() -> None:
    data = {"type": "oauth", "refresh": "r", "access": "a", "expires": 1700000000000, "scope": "user"}
    credential = credential_from_json(data)
    assert isinstance(credential, OAuthCredential)
    assert credential.extra == {"scope": "user"}
    assert credential.to_json() == data


def test_credential_from_json_rejects_unknown_or_malformed_entries() -> None:
    with pytest.raises(AuthStorageError):
        credential_from_json({"type": "nope"})
    with pytest.raises(AuthStorageError):
        credential_from_json({"type": "oauth", "refresh": "r", "access": "a"})


def test_oauth_expiry_respects_minimum_validity() -> None:
    now = 1_000_000
    credential = OAuthCredential(refresh="r", access="a", expires=now + 60_000)
    assert credential.is_expired(now_ms=now) is False
    assert credential.is_expired(now_ms=now, minimum_validity_ms=120_000) is True


# --------------------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------------------


def test_in_memory_store_returns_copies() -> None:
    store = InMemoryCredentialStore()
    store.set("anthropic", ApiKeyCredential(key="sk-a", env={"A": "1"}))
    first = store.get("anthropic")
    assert first is not None
    first.env["A"] = "mutated"
    assert store.get("anthropic").env == {"A": "1"}
    assert [(info.provider_id, info.type) for info in store.list()] == [("anthropic", "api_key")]
    store.delete("anthropic")
    assert store.get("anthropic") is None


def test_file_store_round_trip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "agent" / "auth.json"
    store = FileCredentialStore(path)
    store.set("anthropic", ApiKeyCredential(key="sk-a"))
    store.set("github-copilot", OAuthCredential(refresh="r", access="a", expires=123))

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "anthropic": {"type": "api_key", "key": "sk-a"},
        "github-copilot": {"type": "oauth", "refresh": "r", "access": "a", "expires": 123},
    }

    reopened = FileCredentialStore(path)
    assert reopened.get("anthropic") == ApiKeyCredential(key="sk-a")
    assert [info.provider_id for info in reopened.list()] == ["anthropic", "github-copilot"]
    assert [info.type for info in reopened.list()] == ["api_key", "oauth"]

    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600

    reopened.delete("anthropic")
    assert FileCredentialStore(path).get("anthropic") is None
    # The atomic write must not leave temporary files behind.
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["auth.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_file_store_preserves_an_existing_mode(tmp_path: Path) -> None:
    # Upstream passes `mode` only on creation so administrator-managed modes and
    # ACLs survive a write.
    path = _write(tmp_path / "auth.json", "{}")
    path.chmod(0o640)
    store = FileCredentialStore(path)
    store.set("openai", ApiKeyCredential(key="sk-o"))
    assert (path.stat().st_mode & 0o777) == 0o640


def test_file_store_rejects_invalid_documents(tmp_path: Path) -> None:
    path = _write(tmp_path / "auth.json", '{"anthropic": {"type": "bogus"}}')
    with pytest.raises(AuthStorageError):
        FileCredentialStore(path).get("anthropic")

    bad_json = _write(tmp_path / "bad.json", "{not json")
    with pytest.raises(AuthStorageError):
        FileCredentialStore(bad_json).list()


def test_missing_auth_file_is_an_empty_store(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "absent.json")
    assert store.get("anthropic") is None
    assert store.list() == []


# --------------------------------------------------------------------------------------
# Resolution order
# --------------------------------------------------------------------------------------


def test_resolution_order_runtime_then_stored_then_environment() -> None:
    auth = AuthStorage(env={"ANTHROPIC_API_KEY": "env-key"})
    assert auth.resolve_api_key("anthropic") == "env-key"
    assert auth.resolve("anthropic").source == "ANTHROPIC_API_KEY"

    auth.set_api_key("anthropic", "stored-key")
    assert auth.resolve_api_key("anthropic") == "stored-key"
    assert auth.resolve("anthropic").source == "stored credential"

    auth.set_runtime_api_key("anthropic", "runtime-key")
    assert auth.resolve_api_key("anthropic") == "runtime-key"
    assert auth.resolve("anthropic").source == "runtime"

    auth.remove_runtime_api_key("anthropic")
    assert auth.resolve_api_key("anthropic") == "stored-key"

    auth.delete_credential("anthropic")
    assert auth.resolve_api_key("anthropic") == "env-key"


def test_stored_api_key_owns_the_provider_over_the_environment() -> None:
    auth = AuthStorage(env={"OPENAI_API_KEY": "env-key"})
    auth.set_api_key("openai", "stored-key")
    assert auth.resolve_api_key("openai") == "stored-key"


def test_stored_oauth_credential_never_falls_back_to_the_environment() -> None:
    auth = AuthStorage(env={"ANTHROPIC_API_KEY": "env-key"})
    future = int(time.time() * 1000) + 3_600_000
    auth.set_credential("anthropic", OAuthCredential(refresh="r", access="access-token", expires=future))

    resolved = auth.resolve("anthropic")
    assert resolved is not None
    assert resolved.source == "OAuth"
    assert resolved.api_key == "access-token"
    assert resolved.api_key != "env-key"

    # An expired credential is still "configured" but resolves to no key: OAuth
    # refresh belongs to a later layer, and env must not silently replace it.
    auth.set_credential("anthropic", OAuthCredential(refresh="r", access="stale", expires=1))
    assert auth.resolve_api_key("anthropic") is None
    assert auth.has_auth("anthropic") is True


def test_keyless_api_key_credential_falls_back_like_upstream() -> None:
    # envApiKeyAuth only short-circuits on a non-empty `credential.key`.
    auth = AuthStorage(env={"OPENAI_API_KEY": "env-key"})
    auth.set_credential("openai", ApiKeyCredential(key=None))
    assert auth.resolve_api_key("openai") == "env-key"

    without_env = AuthStorage(env={}, store=auth.store)
    assert without_env.resolve_api_key("openai") is None


def test_auth_storage_reads_and_writes_through_a_file_store(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path / "auth.json")
    auth = AuthStorage(store, env={})
    assert auth.resolve_api_key("anthropic") is None

    auth.set_credential("anthropic", ApiKeyCredential(key="sk-file"))
    assert auth.resolve_api_key("anthropic") == "sk-file"
    assert auth.list_configured_providers() == ["anthropic"]

    # A fresh facade over a fresh file store sees the persisted credential.
    reopened = AuthStorage(FileCredentialStore(tmp_path / "auth.json"), env={})
    assert reopened.resolve_api_key("anthropic") == "sk-file"

    auth.set_runtime_api_key("anthropic", "sk-runtime")
    assert auth.resolve_api_key("anthropic") == "sk-runtime"
    auth.delete_credential("anthropic")
    assert auth.resolve_api_key("anthropic") is None
    assert reopened.resolve_api_key("anthropic") is None
    assert auth.list_configured_providers() == []


def test_lazy_package_exports_are_present() -> None:
    import pi_ai

    for name in (
        "AuthStorage",
        "Credential",
        "CredentialStore",
        "FileCredentialStore",
        "InMemoryCredentialStore",
        "ModelRegistry",
        "get_model",
        "load_models_json",
    ):
        assert getattr(pi_ai, name) is not None, name


def test_environment_variables_use_the_documented_priority_order() -> None:
    both = AuthStorage(env={"GEMINI_API_KEY": "gemini", "GOOGLE_API_KEY": "google"})
    assert both.resolve("google").source == "GEMINI_API_KEY"
    google_only = AuthStorage(env={"GOOGLE_API_KEY": "google"})
    assert google_only.resolve_api_key("google") == "google"


def test_blank_environment_values_count_as_unset() -> None:
    auth = AuthStorage(env={"OPENAI_API_KEY": "   "})
    assert auth.resolve_api_key("openai") is None


def test_unknown_provider_has_no_environment_mapping() -> None:
    auth = AuthStorage(env={"ANTHROPIC_API_KEY": "env-key"})
    assert auth.resolve_api_key("some-local-server") is None


def test_list_configured_providers_merges_store_and_runtime_overrides() -> None:
    auth = AuthStorage(env={})
    assert auth.list_configured_providers() == []

    auth.set_api_key("openai", "sk-o")
    auth.set_runtime_api_key("anthropic", "sk-a")
    assert auth.list_configured_providers() == ["anthropic", "openai"]
    assert {info.type for info in auth.list_credentials()} == {"api_key"}

    auth.delete_credential("anthropic")
    assert auth.list_configured_providers() == ["openai"]


# --------------------------------------------------------------------------------------
# JSONC parsing
# --------------------------------------------------------------------------------------


def test_load_models_json_parses_comments_and_trailing_commas(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        """
        {
          // providers are keyed by id
          "providers": {
            "ollama": {
              /* local server */
              "baseUrl": "http://localhost:11434/v1",
              "api": "openai-completions",
              "compat": { "supportsDeveloperRole": false, "thinkingFormat": "openai" },
              "models": [
                {
                  "id": "llama3.1:8b", // a colon in the id, not a thinking level
                  "reasoning": false,
                  "compat": { "thinkingFormat": "ollama" },
                },
              ],
            },
          },
        }
        """,
    )
    overlay = load_models_json(path)
    provider = overlay["ollama"]
    assert provider["base_url"] == "http://localhost:11434/v1"
    assert provider["api"] == "openai-completions"

    model = provider["models"][0]
    assert model["id"] == "llama3.1:8b"
    assert model["provider"] == "ollama"
    # Provider-level and model-level compat are shallow-merged, model keys win.
    assert model["compat"] == {"supportsDeveloperRole": False, "thinkingFormat": "ollama"}


def test_jsonc_scanner_leaves_comment_like_text_inside_strings_alone(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        '{"providers": {"p": {"baseUrl": "https://example.com//v1", "models": [{"id": "a//b"}]}}}',
    )
    overlay = load_models_json(path)
    assert overlay["p"]["base_url"] == "https://example.com//v1"
    assert overlay["p"]["models"][0]["id"] == "a//b"


def test_load_models_json_rejects_entries_missing_id(tmp_path: Path) -> None:
    path = _write(tmp_path / "models.json", '{"providers": {"ghost": {"models": [{"name": "nope"}]}}}')
    with pytest.raises(ModelsConfigError):
        load_models_json(path)


def test_load_models_json_rejects_malformed_documents(tmp_path: Path) -> None:
    path = _write(tmp_path / "models.json", "{ not json }")
    with pytest.raises(ModelsConfigError):
        load_models_json(path)

    scalar = _write(tmp_path / "scalar.json", "[]")
    with pytest.raises(ModelsConfigError):
        load_models_json(scalar)


# --------------------------------------------------------------------------------------
# Builtin catalog
# --------------------------------------------------------------------------------------


def test_builtin_catalog_is_small_and_real() -> None:
    assert set(BUILTIN_CATALOG) == {"anthropic", "openai", "google", "deepseek", "openrouter", "mistral"}
    for provider, models in BUILTIN_CATALOG.items():
        assert 2 <= len(models) <= 5, provider
        for model in models.values():
            assert model.provider == provider
            assert model.api and model.base_url
            assert model.context_window > 0 and model.max_tokens > 0
            assert model.cost.input >= 0 and model.cost.output >= 0


def test_get_model_reads_a_builtin_and_misses_cleanly(tmp_path: Path) -> None:
    # Exact values come from the builtin pack, so bypass any real user overlay.
    models_path = _missing_models_path(tmp_path)
    model = get_model("anthropic", "claude-sonnet-4-5", models_path=models_path)
    assert model is not None
    assert model.provider == "anthropic"
    assert model.api == "anthropic-messages"
    assert model.base_url == "https://api.anthropic.com"
    assert model.cost.input == 3.0
    assert model.cost.output == 15.0
    assert model.context_window == 1_000_000
    assert model.reasoning is True

    assert get_model("anthropic", "no-such-model", models_path=models_path) is None
    assert get_model("no-such-provider", "claude-sonnet-4-5", models_path=models_path) is None

    # The zero-argument form (shared default registry) resolves builtins too.
    assert get_model("anthropic", "claude-sonnet-4-5") is not None


def test_builtin_catalog_keeps_tiers_and_thinking_maps() -> None:
    gpt = BUILTIN_CATALOG["openai"]["gpt-5.5"]
    assert gpt.cost.tiers and gpt.cost.tiers[0].input_tokens_above == 272_000
    assert gpt.thinking_level_map["high"] == "high"
    assert gpt.thinking_level_map["max"] is None


# --------------------------------------------------------------------------------------
# Overlay merge
# --------------------------------------------------------------------------------------


def test_overlay_applies_provider_layer_and_adds_models(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        json.dumps(
            {
                "providers": {
                    "anthropic": {
                        "baseUrl": "https://proxy.example.com",
                        "compat": {"supportsStrictTools": False},
                    },
                    "ollama": {
                        "baseUrl": "http://localhost:11434/v1",
                        "api": "openai-completions",
                        "models": [
                            {
                                "id": "llama3.1:8b",
                                "name": "Llama 3.1 8B (Local)",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 128000,
                                "maxTokens": 32000,
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            }
                        ],
                    },
                }
            }
        ),
    )
    registry = ModelRegistry(auth=AuthStorage(env={}), models_path=path)

    # The provider layer overrides every existing model of that provider...
    sonnet = registry.get("anthropic", "claude-sonnet-4-5")
    assert sonnet.base_url == "https://proxy.example.com"
    assert sonnet.compat["supportsStrictTools"] is False
    assert registry.get("anthropic", "claude-haiku-4-5").base_url == "https://proxy.example.com"
    # ...but leaves other providers alone.
    assert registry.get("openai", "gpt-5.5").base_url == "https://api.openai.com/v1"

    ollama = registry.get("ollama", "llama3.1:8b")
    assert ollama is not None
    assert ollama.api == "openai-completions"
    assert ollama.context_window == 128000
    assert ollama.max_tokens == 32000
    assert "ollama" in registry.get_providers()


def test_overlay_same_id_replaces_the_builtin_model(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        json.dumps({"providers": {"anthropic": {"models": [{"id": "claude-sonnet-4-5", "cost": {"input": 0.5}}]}}}),
    )
    registry = ModelRegistry(auth=AuthStorage(env={}), models_path=path)
    model = registry.get("anthropic", "claude-sonnet-4-5")
    assert model is not None
    assert model.cost.input == 0.5  # overlay wins
    assert model.cost.output == 15.0  # unspecified fields are inherited
    assert model.reasoning is True
    assert len(registry.get_provider_models("anthropic")) == len(BUILTIN_CATALOG["anthropic"])


def test_overlay_new_model_uses_upstream_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        json.dumps(
            {
                "providers": {
                    "local": {"baseUrl": "http://localhost:8080/v1", "api": "openai-completions",
                              "models": [{"id": "tiny"}]}
                }
            }
        ),
    )
    registry = ModelRegistry(auth=AuthStorage(env={}), models_path=path)
    model = registry.get("local", "tiny")
    assert model is not None
    assert model.name == "tiny"
    assert model.reasoning is False
    assert model.input == ["text"]
    assert model.context_window == 128_000
    assert model.max_tokens == 16_384
    assert model.cost.input == 0.0


def test_overlay_model_without_api_or_base_url_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path / "models.json", json.dumps({"providers": {"ghost": {"models": [{"id": "x"}]}}}))
    with pytest.raises(ModelsConfigError):
        ModelRegistry(auth=AuthStorage(env={}), models_path=path)


def test_get_model_honours_an_explicit_models_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        json.dumps(
            {
                "providers": {
                    "ollama": {"baseUrl": "http://localhost:11434/v1", "api": "openai-completions",
                               "models": [{"id": "llama3.1:8b"}]}
                }
            }
        ),
    )
    overlay_model = get_model("ollama", "llama3.1:8b", models_path=path)
    assert overlay_model is not None
    assert overlay_model.base_url == "http://localhost:11434/v1"
    # An explicit path does not hide the builtin catalog.
    assert get_model("anthropic", "claude-sonnet-4-5", models_path=path) is not None


# --------------------------------------------------------------------------------------
# Pattern resolution
# --------------------------------------------------------------------------------------


def test_resolve_provider_id_wildcard_and_thinking_suffix(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    exact = registry.resolve("anthropic/claude-sonnet-4-5")
    assert [match.model.id for match in exact] == ["claude-sonnet-4-5"]
    assert exact[0].thinking_level is None

    with_level = registry.resolve("anthropic/claude-sonnet-4-5:high")
    assert with_level[0].thinking_level == "high"
    assert with_level[0].model.id == "claude-sonnet-4-5"

    wildcard = registry.resolve("anthropic/*")
    assert [match.model.id for match in wildcard] == [
        "claude-sonnet-4-5",
        "claude-opus-4-5",
        "claude-haiku-4-5",
        "claude-sonnet-5",
    ]
    assert all(match.model.provider == "anthropic" for match in wildcard)

    leveled_wildcard = registry.resolve("anthropic/*:low")
    assert len(leveled_wildcard) == len(wildcard)
    assert all(match.thinking_level == "low" for match in leveled_wildcard)

    # Unknown requests come back empty rather than raising.
    assert registry.resolve("anthropic/does-not-exist") == []
    assert registry.resolve("nope/*") == []


def test_resolve_keeps_colons_that_are_not_thinking_levels(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "models.json",
        json.dumps(
            {
                "providers": {
                    "ollama": {"baseUrl": "http://localhost:11434/v1", "api": "openai-completions",
                               "models": [{"id": "llama3.1:8b", "reasoning": True}]}
                }
            }
        ),
    )
    registry = ModelRegistry(auth=AuthStorage(env={}), models_path=path)

    colons = registry.resolve("ollama/llama3.1:8b")
    assert [match.model.id for match in colons] == ["llama3.1:8b"]
    assert colons[0].thinking_level is None

    suffixed = registry.resolve("ollama/llama3.1:8b:high")
    assert [match.model.id for match in suffixed] == ["llama3.1:8b"]
    assert suffixed[0].thinking_level == "high"


def test_resolve_bare_model_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    matches = registry.resolve("claude-sonnet-4-5")
    assert [match.model.provider for match in matches] == ["anthropic"]


# --------------------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------------------


async def test_get_available_is_empty_without_configured_auth(tmp_path: Path) -> None:
    registry = _registry(tmp_path, auth=AuthStorage(env={}))
    assert await registry.get_available() == []
    assert registry.resolve("anthropic/*", available_only=True) == []


async def test_get_available_keeps_only_authenticated_providers(tmp_path: Path) -> None:
    auth = AuthStorage(store=InMemoryCredentialStore(), env={})
    auth.set_api_key("anthropic", "sk-anthropic")
    registry = ModelRegistry(auth=auth, models_path=_missing_models_path(tmp_path))

    available = await registry.get_available()
    assert {model.provider for model in available} == {"anthropic"}
    assert len(available) == len(BUILTIN_CATALOG["anthropic"])
    assert registry.has_configured_auth("anthropic") is True
    assert registry.has_configured_auth("openai") is False


async def test_get_available_sees_environment_keys(tmp_path: Path) -> None:
    registry = _registry(tmp_path, auth=AuthStorage(env={"OPENAI_API_KEY": "sk-env"}))
    available = await registry.get_available()
    assert {model.provider for model in available} == {"openai"}
    assert [match.model.provider for match in registry.resolve("openai/*", available_only=True)] == [
        "openai"
    ] * len(BUILTIN_CATALOG["openai"])


async def test_runtime_override_makes_an_unauthenticated_provider_available(tmp_path: Path) -> None:
    auth = AuthStorage(env={})
    registry = ModelRegistry(auth=auth, models_path=_missing_models_path(tmp_path))
    assert await registry.get_available() == []

    auth.set_runtime_api_key("deepseek", "sk-deepseek")
    available = await registry.get_available()
    assert {model.provider for model in available} == {"deepseek"}
