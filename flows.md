# Flow Map — OWUI Async Context Compression v1.6.1

Complete flow-by-flow walkthrough of `v1.6.1.py` (4,036 lines), covering every function, class, and module-level statement. Companion to `assessment.md`. Line numbers refer to the built file; the same code lives in the `src/` fragments (build is pure concatenation).

## What this plugin is

An Open WebUI **filter** (`Filter` class, line 3095) that operates around chat requests:

1. **Inlet** (before LLM call): inject a stored summary in place of old history, trim tool outputs, inject cross-chat references, hard-cap context to the model window.
2. **Outlet** (after LLM response): in the background, count tokens and — over threshold — generate/save a new rolling summary.
3. **Persistence**: a `chat_summary` table (one row per chat) storing the summary + how many original messages it covers.

---

## Flow 0 — Module load & init (lines 1–490, 3098–3117)

- **Imports (15–52):** pydantic/typing/sqlalchemy always; `open_webui.*` and `tiktoken` optional (guarded imports). `Chats` and `owui_db` may be `None`.
- **i18n tables (66–95):** en-US / zh-CN string dicts.
- **DB discovery (357–432):** `_discover_owui_engine` walks `get_db_context`/`get_db` → `get_bind`/`bind`/`engine`, then top-level `engine`/`bind` attrs. `_discover_owui_schema` tries `Base.metadata.schema`, `metadata_obj.schema`, then `open_webui.env.DATABASE_SCHEMA`.
- **Version shim (434–468):** `_owui_version_ge` compares `open_webui.env.VERSION`. `_call_db` awaits on ≥0.9.0, calls sync otherwise. `_call_db_sync` does the inverse (runs an async method in a 1-worker `ThreadPoolExecutor` via `asyncio.run`).
- **`ChatSummary` model (470–489):** SQLAlchemy table, optional schema.
- **`Filter.__init__` (3098–3117):** stores `owui_db`, engine, builds a fallback `sessionmaker`, seeds `fallback_map` for en-variants, creates empty `_chat_locks` / `_pending_inlet_messages`, calls `_init_database`.
- **`_init_database` (564–592):** `inspect(engine).has_table("chat_summary")`; creates the table if missing.

## Flow 1 — Token counting (163–355)

- **Fast path:** `_estimate_text_tokens` (199, `lru_cache 4096`) — pure C-string heuristics. ASCII: codeish-vs-prose formula. Non-ASCII: `_sample_script_mix` (163) samples up to 256 chars, classifies into han/kana/hangul/cyr/arabic/thai/other, picks a dominant script (or "mixed"), then applies per-script byte coefficients.
- **Exact path:** `_get_cached_tokens` (265, `lru_cache 1024`) — tiktoken `o200k_base` if available, else falls back to the estimator.
- **Wrappers:** `_count_tokens` (exact), `_estimate_content_tokens`/`_estimate_messages_tokens` (fast), `_calculate_messages_tokens` (exact, with debug timing). `_extract_text_content` (291) flattens str / dict / multimodal list to text.

## Flow 2 — i18n (97–161)

`_get_translation` → `_resolve_language`: direct match → `fallback_map` → base-language prefix match → `en-US`. Then dict lookup with en-US fallback, then `str.format` with kwargs (format errors swallowed with a warning).

## Flow 3 — Inlet (3203–3915) — the main request path

1. **Skip check (3212):** `_should_skip_compression` — copilot model / copilot_sdk pipe → return body untouched.
2. **User context (3218):** `_get_user_context` (1376–1435) — normalizes `__user__` (list/tuple/dict), pulls id/name/language, and if `__event_call__` is present runs a 2s-timeout JS snippet in the browser to get the real frontend locale.
3. **Tool-call normalization (3222):** `_normalize_native_tool_call_ids` (733) shortens overlong `tool_call.id` (sha1 suffix, 40-char cap) via `_shorten_tool_call_id` (720) and rewrites matching `tool_call_id` on tool messages so links stay aligned.
4. **Tool-output trimming (3238–3271):** only if valve enabled **and** `_get_function_calling_mode` (1100) == `"native"`. `_trim_native_tool_outputs` (773):
   - `_get_atomic_groups` (935) groups `assistant(tool_calls) → tool(s) → assistant(followup)` into atomic units.
   - For each native group whose total tool chars ≥ `tool_trim_threshold_chars`, replaces tool contents with a "[Content collapsed]" marker and tags metadata; wraps the assistant follow-up with "[Tool outputs trimmed]".
   - Second pass: regex-collapses large `result="..."` inside legacy `<details type="tool_calls">` HTML blocks.
   - Optional `collect_debug` stats dict.
5. **Chat context (3273):** `_get_chat_context` (1544) resolves `chat_id`/`message_id` from body → body.metadata → `__metadata__`.
6. **External references (3277–3283):** `_handle_external_chat_references` (2628) — see Flow 6. May set `body["__external_references__"]`.
7. **System-prompt extraction (3289–3410):** tries the model's DB `params.system` (custom model), else first `role=system` message. Builds `system_prompt_msg` for budget math.
8. **Message stats + debug config logging (3413–3478).**
9. **Target boundary (3481):** `_calculate_target_compressed_count` (1268) — in original-history coordinates, aligned to an atomic boundary.
10. **Load stored summary (3484):** `_load_summary_record` → **two branches:**
    - **Summary exists (3491–3846):**
      - Clamp `compressed_count`; build **head** = first `effective_keep_first` msgs (`_get_effective_keep_first` 1364 protects first N non-system + interleaved system), **start_index** aligned via `_align_tail_start_to_atomic_boundary` (988), **preserved system msgs** from the gap, **summary_msg** via `_build_summary_message` (1209, localized prefix/suffix + `covered_until`).
      - If external refs present, prepend `<external_references>` block to the summary msg.
      - **Budget:** fast estimate → if ≥85% of `max_context_tokens`, exact count; then a **while-loop dropping oldest atomic groups** from the tail until under the limit.
      - Token section stats, then **status emission** (`_should_show_status` 3070 + usage translation, high-usage suffix >90%).
    - **No summary (3847–3914):**
      - If external refs present, inject a synthetic `is_external_references` assistant message.
      - Same budget math; if over limit, drop oldest atomic groups **but preserve system msgs and external-ref messages** (kept in a preserved list / re-inserted).
      - Status emission.
11. **Finalize (3916–3949):** set `body["messages"]`, `_capture_pending_inlet_messages` (1136 — deep-copies transient inlet-only ref messages by chat_id), strip chat-type files from `metadata.files` (prevents RAG), emit "injected N references" status.

## Flow 4 — Outlet (3922–4036)

1. **Skip check (3930).**
2. **User context + chat_id + model + messages.**
3. **Unfold (3959–3984):** if native mode, prefer DB-loaded messages when they're longer, then `_unfold_messages` (1040) reverse-expands compact assistant `output` dicts via OpenWebUI's `convert_output_to_messages`.
4. **Restore pending (3990):** `_restore_pending_inlet_messages` (1159) re-inserts the transient ref messages captured in inlet, deduped by role+content, at the `covered_until` index.
5. **Target boundary (3997):** `_calculate_target_compressed_count`.
6. **Lock + spawn (4012–4036):** `_get_chat_lock` (1130) per-chat `asyncio.Lock`; if already locked, skip; else `asyncio.create_task(_locked_summary_task)` (1770) → `_check_and_generate_summary_async` (1784).

## Flow 5 — Background summary (1790–2623)

`_check_and_generate_summary_async` (1784):
1. Fast estimate; if <85% of `compression_threshold_tokens`, use estimate, else exact count in a thread.
2. Emit context-usage status.
3. If `current_tokens >= compression_threshold_tokens` → `_generate_summary_async` (1816), else log "not reached".

`_generate_summary_async` (1816):
1. Resolve `target_compressed_count` (or compute).
2. Determine **middle slice** (what to compress) vs **tail** (kept), using `_get_summary_view_state` (1237) to find the existing summary marker + `base_progress`.
3. **Fit to budget:** resolve summary model (`summary_model` valve or chat model), `_compute_summary_request_limits` (2257) reserves safety margin + output budget. A **while-loop shrinks** the middle slice: drop newest atomic group → drop embedded summary marker → drop DB previous-summary → give up.
4. **Previous summary:** loaded from DB only when there's no summary marker in the messages.
5. **Format + prompt:** `_format_messages_for_summary` (2286, keeps IDs/names/metadata), `_build_summary_prompt` (2320 — the large XML "working memory" system prompt with 16 rules + output schema).
6. **Emit "generating" status**, call `_call_summary_llm` (2462).
7. **Save:** `_save_summary` (611) with `saved_compressed_count` in original-history coordinates.
8. **Post-save status** (loaded summary count), then a **token-usage recompute**: fetches the model's system prompt from DB, rebuilds the next context (head + new summary marker + tail), exact-counts it, emits updated usage status.

`_call_summary_llm` (2462):
- Builds payload (model, single user message = prompt, `max_tokens`, `temperature`, optional `chat_template_kwargs` from `_get_summary_chat_template_kwargs` (2441)). Fetches the user object via `_call_db(Users.get_user_by_id)`, calls OpenWebUI's `generate_chat_completion` with the injected `Request` (synthetic fallback scope if none).
- Handles `JSONResponse`-style bodies, `_extract_provider_error` (2410) for upstream errors, validates `choices`, returns `content`.

## Flow 6 — External chat references (2628–2969)

`_handle_external_chat_references` (2628):
- If `metadata.files` has `type=="chat"` entries:
  - Compute a **direct-injection budget** = `max_context_tokens − tokens(base messages)`.
  - For each referenced chat: try `_load_summary_record` → use stored summary if present; else `_load_full_chat_messages` (1330) → `_reconstruct_active_history_branch` (1293, walks `parentId` chain from `currentId`, or timestamp-sorts).
  - If the full chat fits the remaining budget → inject verbatim; else generate a summary via `_call_summary_llm` (truncating via `_truncate_messages_for_summary` 2235 if over the summary model's window), fall back to direct injection on failure, trim to `max_summary_tokens`, and cache the summary back to the referenced chat's DB row.
  - Wrap all in `<referenced_chats>` blocks, stash in `body["__external_references__"]` for inlet to mount.
- **`_generate_referenced_summaries_background` (2877): dead code — defined but never called** (largest single lean win in `assessment.md`).

## Flow 7 — DB sessions (495–563)

- `_async_db_session` (495): prefers `get_async_db_context`/`get_async_db`; falls back to wrapping the sync `_sync_db_session` (521) for <0.9.0.
- `_sync_db_session` (521): `get_db_context`/`get_db` → `SessionLocal`/`ScopedSession` → fallback `sessionmaker`.
- `_save_summary` (611) / `_load_summary_record` (699) detect async-vs-sync sessions via `iscoroutinefunction(session.execute)` and branch to SQLAlchemy 2.0 async or legacy `query`. `_save_summary` uses an optimistic guard (skip if new count ≤ stored count).

## Flow 8 — Console / logging (2972–3094)

- `_log` (3038) → backend `logger.info` if `debug_mode`, then always `_emit_frontend_console_log` (2974).
- `_emit_frontend_console_log`: gated by `show_debug_log` unless `force`; strips `====`/`----` separator lines; if multi-line, emits a collapsible `console.groupCollapsed` JS snippet, else a single `console.{log|error|warn}`; 2s timeout; on a "broadcast" `ValueError` it disables `show_debug_log` for the session.
- `_should_show_status` (3070): respects `show_token_usage_status` + `token_usage_status_threshold` (0 = always).

---

## Cross-cutting observations

- **Every flow funnels through `_get_model_thresholds` (1477)** for limits, and through `_get_summary_model_context_limit` (1532) for summary budget — both resolve per-model overrides → base-model → global valve.
- **The "atomic group" concept is the load-bearing invariant**: `_get_atomic_groups` is used by trimming, boundary alignment, budget-shrinking in both inlet branches, and in the background shrink loop. Keeping tool-call chains intact is the plugin's core correctness property.
- **Two coordinate systems** are maintained deliberately: *visible message indices* (inlet/outlet) vs *original-history count* (`compressed_message_count` / `covered_until`), bridged by `_get_summary_view_state` / `_get_original_history_count` / `_calculate_target_compressed_count`.
- **Confirmed dead/unused** (matches `assessment.md`): `_generate_referenced_summaries_background`, `import traceback`, `import json as json_module`, `_get_chat_context`'s `message_id`, `_get_user_context`'s `user_id`/`user_name`, `_load_summary`'s `body` param, `_should_skip_compression`'s `__model__` param, and the identical-branch `name_part` in `_truncate_messages_for_summary`.
