# Flow Map — OWUI Async Context Compression v1.6.1

Complete flow-by-flow walkthrough of `v1.6.1.py`, covering every function, class,
and module-level statement. Companion to `assessment.md`.

References are by **method name + `src/` fragment** (not line numbers, which go
stale every refactor). The built file is a pure concatenation of the fragments in
`build.py` order: `_header`, `i18n`, `tokens`, `db`, `toolcalls`, `compression`,
`summarize`, `externalrefs`, `console`, `filter`.

## What this plugin is

An Open WebUI **filter** (`Filter` class in `src/filter.py`) that operates around
chat requests:

1. **Inlet** (before LLM call): inject a stored summary in place of old history, trim tool outputs, inject cross-chat references, hard-cap context to the model window.
2. **Outlet** (after LLM response): in the background, count tokens and — over threshold — generate/save a new rolling summary.
3. **Persistence**: a `chat_summary` table (one row per chat) storing the summary + how many original messages it covers.

---

## Flow 0 — Module load & init

- **Imports (`src/_header.py`):** pydantic/typing/sqlalchemy always; `open_webui.*` and `tiktoken` optional (guarded imports). `Chats` and `owui_db` may be `None`.
- **i18n tables (`src/i18n.py`):** en-US / zh-CN string dicts.
- **DB discovery (`src/db.py`):** `_discover_owui_engine` walks `get_db_context`/`get_db` → `get_bind`/`bind`/`engine`, then top-level `engine`/`bind` attrs. `_discover_owui_schema` tries `Base.metadata.schema`, `metadata_obj.schema`, then `open_webui.env.DATABASE_SCHEMA`.
- **Version shim (`src/db.py`):** `_owui_version_ge` compares `open_webui.env.VERSION`. `_call_db` awaits on ≥0.9.0, calls sync otherwise. `_call_db_sync` does the inverse (runs an async method in a lazily-created **shared** single-worker pool `_get_db_sync_pool` via `asyncio.run`).
- **`ChatSummary` model (`src/db.py`):** SQLAlchemy table, optional schema.
- **`Filter.__init__` (`src/filter.py`):** stores `owui_db`, engine, builds a fallback `sessionmaker`, seeds `fallback_map` for en-variants, creates empty `_chat_locks` / `_pending_inlet_messages`, initializes `_frontend_broadcast_broken = False`, calls `_init_database`.
- **`_init_database` (`src/db.py`):** `inspect(engine).has_table("chat_summary")`; creates the table if missing.

## Flow 1 — Token counting (`src/tokens.py`)

- **Fast path:** `_estimate_text_tokens` (`lru_cache 4096`) — pure C-string heuristics. ASCII: codeish-vs-prose formula. Non-ASCII: `_sample_script_mix` samples up to 256 chars, classifies into han/kana/hangul/cyr/arabic/thai/other, picks a dominant script (or "mixed"), then applies per-script byte coefficients.
- **Exact path:** `_get_cached_tokens` (`lru_cache 1024`) — tiktoken `o200k_base` if available, else falls back to the estimator.
- **Wrappers:** `_count_tokens` (exact), `_estimate_content_tokens`/`_estimate_messages_tokens` (fast), `_calculate_messages_tokens` (exact, with debug timing). `_extract_text_content` flattens str / dict / multimodal list to text.
- **`_resolve_context_tokens(messages, limit)`** (Phase 2 helper): estimate → if well under the limit (85% margin) use the estimate, else exact count in a thread. Returns `(total, estimated, used_precise)` — the `used_precise` flag replaces the old fragile `total == estimated` equality checks.

## Flow 2 — i18n (`src/i18n.py`)

`_get_translation` → `_resolve_language`: direct match → `fallback_map` → base-language prefix match → `en-US`. Then dict lookup with en-US fallback, then `str.format` with kwargs (format errors swallowed with a warning).

## Flow 3 — Inlet (`src/filter.py`) — the main request path

`inlet` is the request entry point. Its heavy sections are extracted helpers in
the same file, called in this order:

1. **Skip check:** `_should_skip_compression` — copilot model / copilot_sdk pipe → return body untouched.
2. **User context:** `_get_user_context` (`src/compression.py`) — normalizes `__user__` (list/tuple/dict), pulls the language, and if `__event_call__` is present runs a 2s-timeout JS snippet in the browser to get the real frontend locale.
3. **Tool-call normalization:** `_normalize_native_tool_call_ids` (`src/toolcalls.py`) shortens overlong `tool_call.id` (sha1 suffix, 40-char cap) via `_shorten_tool_call_id` and rewrites matching `tool_call_id` on tool messages so links stay aligned.
4. **Tool-output trimming:** only if valve enabled **and** `_get_function_calling_mode` (`src/toolcalls.py`) == `"native"`. `_trim_native_tool_outputs` (`src/toolcalls.py`):
   - `_get_atomic_groups` groups `assistant(tool_calls) → tool(s) → assistant(followup)` into atomic units.
   - For each native group whose total tool chars ≥ `tool_trim_threshold_chars`, replaces tool contents with a "[Content collapsed]" marker and tags metadata; wraps the assistant follow-up with "[Tool outputs trimmed]".
   - Second pass: regex-collapses large `result="..."` inside legacy `<details type="tool_calls">` HTML blocks.
   - Returns the trimmed count; inlet logs it under the debug-log gate.
5. **Chat context:** `_get_chat_context` (`src/compression.py`) resolves `chat_id` from body → body.metadata → `__metadata__`.
6. **External references:** `_handle_external_chat_references` (`src/externalrefs.py`) — see Flow 6. May set `body["__external_references__"]`.
7. **System-prompt extraction → `_extract_inlet_system_prompt`** (Phase 4 helper in `src/filter.py`): tries the model's DB `params.system` via `_extract_system_from_params` (`src/compression.py`), else first `role=system` message. Builds `system_prompt_msg` for budget math; logs message-role stats.
8. **Debug config logging:** model threshold configs, aligned target boundary (`_calculate_target_compressed_count`, original-history coordinates).
9. **Load stored summary** (`_load_summary_record`) → **two branches, one helper each** (Phase 4):
   - **Summary exists → `_assemble_summary_view`:** clamp `compressed_count`; build **head** = first `effective_keep_first` msgs (`_get_effective_keep_first` protects first N non-system + interleaved system), **start_index** aligned via `_align_tail_start_to_atomic_boundary`, **preserved system msgs** from the gap, **summary_msg** via `_build_summary_message` (localized prefix/suffix + `covered_until`). If external refs present, prepend `<external_references>` block to the summary msg. **Budget:** `_resolve_context_tokens` (85% margin) → if precise, a **while-loop dropping oldest atomic groups** (`_drop_oldest_atomic_group`) from the tail until under the limit. Token section stats, then status emission via `_emit_context_usage_status`.
   - **No summary → `_assemble_plain_view`:** if external refs present, inject a synthetic `is_external_references` assistant message. Same budget math; if over limit, drop oldest atomic groups **but preserve system msgs and external-ref messages** (`_drop_oldest_atomic_group` with `preserve_protected=True`). Status emission. Returns `None` for empty history (inlet returns body early).
10. **Finalize:** set `body["messages"]`, `_capture_pending_inlet_messages` (`src/compression.py` — deep-copies transient inlet-only ref messages by chat_id, bounded to 64 chats), strip chat-type files from `metadata.files` (prevents RAG), emit "injected N references" status.

## Flow 4 — Outlet (`src/filter.py`)

1. **Skip check** (`_should_skip_compression`).
2. **User context + chat_id + messages.**
3. **Unfold:** if native mode, prefer DB-loaded messages (`_load_full_chat_messages`) when they're longer, then `_unfold_messages` (`src/toolcalls.py`) reverse-expands compact assistant `output` dicts via OpenWebUI's `convert_output_to_messages`.
4. **Restore pending:** `_restore_pending_inlet_messages` (`src/compression.py`) re-inserts the transient ref messages captured in inlet, deduped by role+content, at the `covered_until` index.
5. **Target boundary:** `_calculate_target_compressed_count`, aligned to an atomic boundary.
6. **Lock + spawn:** `_get_chat_lock` (`src/compression.py`) per-chat `asyncio.Lock` (map bounded at 128 idle locks); if already locked, skip; else `asyncio.create_task(_locked_summary_task)` (`src/compression.py`) → `_check_and_generate_summary_async`.

## Flow 5 — Background summary (`src/compression.py`, `src/summarize.py`)

`_check_and_generate_summary_async` (`src/compression.py`):
1. `_resolve_context_tokens` against the resolved `compression_threshold_tokens` (a percentage of the resolved max context — see cross-cutting notes).
2. Emit context-usage status via `_emit_context_usage_status` (history-only label + "drives compaction" note).
3. If `compression_threshold_tokens > 0` and `current_tokens >= compression_threshold_tokens` → `_generate_summary_async`, else log "not reached" (threshold 0 = max context unknown, never trigger on the threshold alone).

`_generate_summary_async` (`src/summarize.py`) — orchestration only; the heavy
sections are extracted helpers in the same file:

1. **`_resolve_summary_boundary`:** resolve `target_compressed_count` (or compute), determine the **middle slice** (what to compress) vs **tail** (kept) using `_get_summary_view_state` to find the existing summary marker + `base_progress`. Returns `None` when there is nothing to compress.
2. **`_fit_summary_request`:** resolve the summary model (`summary_model` valve or chat model), `_compute_summary_request_limits` reserves safety margin + output budget; load a DB-backed previous summary only when there is no summary marker in the messages; a **while-loop shrinks** the middle slice — drop newest atomic group → drop embedded summary marker → drop DB previous-summary → give up. Returns the fitted request payload, or `None` to skip.
3. **Format + prompt:** `_format_messages_for_summary` (keeps IDs/names/metadata), `_build_summary_prompt` — the large XML "working memory" system prompt with 16 rules + output schema.
4. **Emit "generating" status**, call `_call_summary_llm`.
5. **Save:** `_save_summary` (`src/db.py`) with `saved_compressed_count` in original-history coordinates.
6. **`_emit_post_summary_usage_status`:** post-save status (loaded summary count), then a **token-usage recompute**: fetches the model's system prompt from DB, rebuilds the next context (head + new summary marker + tail), exact-counts it in a thread, emits updated usage status.

`_call_summary_llm` (`src/summarize.py`):
- Builds payload (model, single user message = prompt, `max_tokens`, `temperature`, optional `chat_template_kwargs` from `_get_summary_chat_template_kwargs`). Fetches the user object via `_call_db(Users.get_user_by_id)`, calls OpenWebUI's `generate_chat_completion` with the injected `Request` (synthetic fallback scope if none).
- Handles `JSONResponse`-style bodies, `_extract_provider_error` for upstream errors, validates `choices`, returns `content`.

## Flow 6 — External chat references (`src/externalrefs.py`)

`_handle_external_chat_references`:
- If `metadata.files` has `type=="chat"` entries:
  - Compute a **direct-injection budget** = `max_context_tokens − tokens(base messages)`.
  - For each referenced chat: try `_load_summary_record` → use stored summary if present; else `_load_full_chat_messages` (`src/compression.py`) → `_reconstruct_active_history_branch` (walks `parentId` chain from `currentId`, or timestamp-sorts).
  - If the full chat fits the remaining budget → inject verbatim; else generate a summary via `_call_summary_llm` (truncating via `_truncate_messages_for_summary` if over the summary model's window), fall back to direct injection on failure, trim to `max_summary_tokens`, and cache the summary back to the referenced chat's DB row.
  - Wrap all in `<referenced_chats>` blocks, stash in `body["__external_references__"]` for inlet to mount.

## Flow 7 — DB sessions (`src/db.py`)

- `_async_db_session`: prefers `get_async_db_context`/`get_async_db`; falls back to wrapping the sync `_sync_db_session` for <0.9.0.
- `_sync_db_session`: `get_db_context`/`get_db` → `SessionLocal`/`ScopedSession` → fallback `sessionmaker`.
- `_save_summary` / `_load_summary_record` detect async-vs-sync sessions via `iscoroutinefunction(session.execute)` and branch to SQLAlchemy 2.0 async or legacy `query`. `_save_summary` uses an optimistic guard (skip if new count ≤ stored count).

## Flow 8 — Console / logging (`src/console.py`)

- `_log` → backend `logger.info` if `debug_mode`, then always `_emit_frontend_console_log`.
- `_emit_frontend_console_log`: gated by `show_debug_log` **and** the `_frontend_broadcast_broken` flag unless `force`; strips `====`/`----` separator lines; if multi-line, emits a collapsible `console.groupCollapsed` JS snippet, else a single `console.{log|error|warn}`; 2s timeout; on a "broadcast" `ValueError` it sets `_frontend_broadcast_broken` for the session (the valve itself is no longer mutated).
- `_should_show_status`: respects `show_token_usage_status` + `token_usage_status_threshold` (0 = always).
- `_emit_context_usage_status` (Phase 2 helper): the single choke point for the context-usage status emission — translation, high-usage suffix >90%, status event. Used by the outlet, both inlet branches, and the summary path.

---

## Cross-cutting observations

- **Every flow funnels through `_get_model_thresholds`** for limits, and through `_get_summary_model_context_limit` for summary budget. Resolution chain: per-model `model_thresholds` override (absolute, wins; direct or via base_model_id) → **model-reported context length** (`_resolve_reported_context_length`, `src/contextlength.py` — API-type-dispatched resolver registry, `openai_api` reads `meta.context_length` then `meta.n_ctx`, flattened or nested under `meta`, as stored by Open WebUI from the API payload; meta normalized via `_meta_to_dict` to handle both plain dicts and pydantic `ModelMeta`) → global `max_context_tokens` valve fallback. The compression threshold is `compression_threshold_percent` (default 80) of the resolved max context — the old absolute `compression_threshold_tokens` valve is gone.
- **Self-healing context length:** the stored meta is an import-time snapshot, so inlet spawns `_self_heal_context_length` (fire-and-forget `asyncio.create_task`, gated by the `self_heal_context_length` valve; the first turn of a new chat always checks — tracked in bounded `_context_heal_checked_chats` — other turns rate-limited to one live re-query per model per 5 minutes). It re-queries the enabled OpenAI-compatible connections (`_get_openai_connections` reads `openai.api_base_urls` / `api_keys` / `api_configs` from the config table) via `_live_query_context_length` (httpx, 3s timeout), and when the live value differs it rewrites `meta.context_length` in place via `Models.update_model_by_id` (full read-modify-write form — name/params/base_model_id/is_active preserved). All failures swallowed; a healed value is picked up on the next request's threshold resolution.
- **`_emit_context_usage_status` labels** (Phase 4.6): `label_key` distinguishes full-request counts ("Context Usage" — inlet branches, post-summary) from history-only counts ("History Usage" — outlet), and `note_key` marks the outlet reading as the one that drives compaction.
- **The "atomic group" concept is the load-bearing invariant**: `_get_atomic_groups` is used by trimming, boundary alignment, budget-shrinking in both inlet branches, and in the background shrink loop. Keeping tool-call chains intact is the plugin's core correctness property.
- **Two coordinate systems** are maintained deliberately: *visible message indices* (inlet/outlet) vs *original-history count* (`compressed_message_count` / `covered_until`), bridged by `_get_summary_view_state` / `_get_original_history_count` / `_calculate_target_compressed_count`.
- **Shared status/token helpers** (Phase 2): `_emit_context_usage_status`, `_resolve_context_tokens`, `_extract_system_from_params`, `_drop_oldest_atomic_group` — one definition each, all call sites converted.
- **Structural splits** (Phase 4): `inlet` and `_generate_summary_async` were split into the helpers listed in Flows 3 and 5; each helper's body is a verbatim extraction of the original inline code (verified byte-identical, modulo indentation).
