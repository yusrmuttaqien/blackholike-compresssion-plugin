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


def _meta_to_dict(meta) -> Optional[Dict[str, Any]]:
    """Normalize a model meta payload (plain dict or pydantic model)."""
    if isinstance(meta, dict):
        return meta
    dump = getattr(meta, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            return None
    return None


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
        source = _meta_to_dict(source)
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

    meta = _meta_to_dict(getattr(model_obj, "meta", None))
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


async def _live_query_context_length(connections, model_name: str) -> Optional[int]:
    """Query /v1/models on each (base_url, api_key) connection.

    Returns the context length reported for model_name (context_length
    or n_ctx), or None when the model is not found on any server, the
    matched entry reports no value, or httpx is unavailable.
    """
    try:
        import httpx
    except ImportError:
        return None

    for base_url, api_key in connections:
        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{str(base_url).rstrip('/')}/v1/models", headers=headers
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            continue

        for entry in payload.get("data", []):
            if not isinstance(entry, dict) or entry.get("id") != model_name:
                continue
            nested = (
                entry.get("meta")
                if isinstance(entry.get("meta"), dict)
                else {}
            )
            value = (
                entry.get("context_length")
                or entry.get("n_ctx")
                or nested.get("context_length")
                or nested.get("n_ctx")
            )
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                return int(value)
            return None
    return None


def _get_openai_connections() -> List[tuple]:
    """Enabled OpenAI-compatible connections from the Open WebUI config table.

    Reads openai.api_base_urls / openai.api_keys / openai.api_configs and
    returns (base_url, api_key) pairs for enabled entries, in config
    order. Returns [] when the config table is unreadable.
    """
    if owui_engine is None:
        return []
    try:
        from sqlalchemy import text

        with owui_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT key, value FROM config WHERE key IN "
                    "('openai.api_base_urls', 'openai.api_keys', 'openai.api_configs')"
                )
            ).fetchall()
    except Exception:
        return []

    values = {key: value for key, value in rows}
    try:
        base_urls = json.loads(values.get("openai.api_base_urls") or "[]")
        api_keys = json.loads(values.get("openai.api_keys") or "[]")
        api_configs = json.loads(values.get("openai.api_configs") or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(base_urls, list):
        return []
    if not isinstance(api_keys, list):
        api_keys = []
    if not isinstance(api_configs, dict):
        api_configs = {}

    connections = []
    for i, base_url in enumerate(base_urls):
        config = api_configs.get(str(i))
        if isinstance(config, dict) and not config.get("enable", True):
            continue
        key = api_keys[i] if i < len(api_keys) else ""
        connections.append((base_url, key if isinstance(key, str) else ""))
    return connections
