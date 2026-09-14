"""Built-in provider adapters.

Importing this package is cheap: :func:`register_builtin_apis` pulls the adapter
modules in lazily, so a partially built tree never breaks ``import pi_ai``.
"""

from __future__ import annotations

from ..stream import get_api, register_api

#: ``api`` string → factory producing a ``ProviderStreams``.
BUILTIN_APIS = {
    "anthropic-messages": ("anthropic_messages", "anthropic_messages_api"),
    "openai-completions": ("openai_completions", "openai_completions_api"),
}


def register_builtin_apis() -> None:
    import importlib

    for api_name, (module_name, factory_name) in BUILTIN_APIS.items():
        if get_api(api_name) is not None:
            continue
        module = importlib.import_module(f".{module_name}", __name__)
        register_api(api_name, getattr(module, factory_name)())


__all__ = ["BUILTIN_APIS", "register_builtin_apis"]
