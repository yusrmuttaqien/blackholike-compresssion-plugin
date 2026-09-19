# ── contextlength.py · Model-reported context length resolution ───────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.
#
# A model's max context is taken from the model API wherever possible
# instead of the global valve. Open WebUI stores the model object the
# API returned in the model's `meta`, so for OpenAI-compatible APIs
# (OpenAI, llama.cpp server, vLLM, LM Studio, OpenRouter, ...) no live
# query is needed — `meta.context_length` is authoritative.
#
# To support a new API type:
#   1. Write a resolver: (model_id, model_obj) -> Optional[int]
#   2. Register it in _CONTEXT_LENGTH_RESOLVERS under the API type name.


def _resolve_context_length_openai_api(model_id, model_obj) -> Optional[int]:
    """OpenAI-compatible: Open WebUI stores the /v1/models payload in meta.

    Field precedence: `context_length` (OpenAI standard) then `n_ctx`
    (llama.cpp — the slot context size the server enforces; `n_ctx_train`
    is the trained size and is deliberately ignored). Both are checked at
    the meta top level and one level down under 'meta': llama.cpp nests
    its fields in a 'meta' sub-object, and Open WebUI flattens them into
    the model meta on import, so either shape may occur.
    """

    def _pick(source):
        if not isinstance(source, dict):
            return None
        for key in ("context_length", "n_ctx"):
            value = source.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                return int(value)
        return None

    meta = getattr(model_obj, "meta", None)
    nested = meta.get("meta") if isinstance(meta, dict) else None
    return _pick(meta) or _pick(nested)


# API type -> resolver. Add entries here when supporting new API types
# (e.g. "ollama": ... once an Ollama /api/show resolver exists).
_CONTEXT_LENGTH_RESOLVERS = {
    "openai_api": _resolve_context_length_openai_api,
}

# API types known to Open WebUI connections, used for model-id prefix
# detection below.
_KNOWN_API_TYPES = frozenset(
    {
        "openai_api",
        "ollama",
        "openrouter",
        "lm_studio",
        "vllm",
        "groq",
        "mistral",
        "anthropic",
        "gemini",
        "bedrock",
    }
)


def _detect_api_type(model_id: str) -> str:
    """Best-effort detection of a model's API type.

    Connection model ids are namespaced as 'connection_name/model_id'
    and default connection names match the API type. When no prefix
    match is found, assume OpenAI-compatible (the default for custom
    connections). A Connections-table lookup can replace this later if
    renamed connections prove to be a problem.
    """
    prefix = model_id.split("/", 1)[0] if "/" in model_id else ""
    if prefix in _KNOWN_API_TYPES:
        return prefix
    return "openai_api"


def _resolve_reported_context_length(model_id: str, model_obj) -> Optional[int]:
    """Query the registered resolver for the model's max context tokens.

    Returns None when the model/API does not report one; callers fall
    back to the global max_context_tokens valve.
    """
    if model_obj is None:
        return None
    api_type = _detect_api_type(model_id)
    resolver = _CONTEXT_LENGTH_RESOLVERS.get(
        api_type, _resolve_context_length_openai_api
    )
    try:
        return resolver(model_id, model_obj)
    except Exception:
        return None
