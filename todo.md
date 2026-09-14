# v1.6.1 Refactor — Ordered Plan

Goal: reduce LOC and make the code tidier. **Remove first, then tidy.**
Baseline: `v1.6.1.py` = **4,813 lines**.

**Phase 1 DONE → 3,932 lines (−881, no functional loss).**
Phase 2: nothing to delete (both features confirmed in use).
**Phase 3 DONE → split into `src/` modules + `build.py` (verified byte-identical).**

---

## Phase 1 — Cut non-essentials (biggest wins, lowest risk)

### [x] 1.1 Move docstring header → README.md  (−303 lines) ✅
- Docstring body (lines 2–302) → `v1.6.1/README.md` (301 lines).
- Replaced with a 10-line header pointing to README.md. Zero code risk.

### [x] 1.2 Strip debug/diagnostic block  (−561 lines) ✅
- Deleted the 8 methods + all 6 call-site blocks (inlet ×4, outlet ×2).
- **Kept** `_log` and `_emit_frontend_console_log`.
- ⚠️ Caught & fixed: `_unfold_messages` + `_get_function_calling_mode` were
  interleaved in the same line range and got deleted by accident → **restored**
  from `v1.6.1.py.bak`. Verified via method-set diff: exactly 8 removed, 0 added,
  all `self._*` calls resolve.
- Note: `trim_debug`/`collect_debug` plumbing left in place (harmless; possible
  future cleanup).

### [x] 1.3 Collapse i18n to 2 locales  (−108 lines) ✅
- `TRANSLATIONS` now has only **en-US + zh-CN** (11 keys each, verified complete).
- Removed 7 locales; trimmed `fallback_map` to en-variants only.
- `_resolve_language` is dynamic (driven by `TRANSLATIONS` keys) → removed locales
  safely fall back to `en-US`.

> **Phase 1 complete: 4,813 → 3,932 lines (−881).** No behavior change.
> `v1.6.1.py.bak` = original kept as safety backup; delete once you've verified.

---

## Phase 2 — Feature decisions  ✅ RESOLVED: keep everything

### [x] 2.1 External chat-references  — **KEEP** (confirmed in use)
- User references other chats from the OWUI input box; plugin reads + summarizes + injects them.
- Trigger: `metadata.files` entries with `type == "chat"` (line ~1659).

### [x] 2.2 Tool-output trimming  — **KEEP** (confirmed: user uses native tool calling)

> **No deletions in Phase 2.**

---

## Phase 3 — Tidy / split  ✅ DONE

### [x] 3.1 Split into modules
- `src/` fragments: `_header`, `i18n`, `tokens`, `db`, `toolcalls`, `compression`,
  `summarize`, `externalrefs`, `console`, `filter`.
- `Filter` split into 8 mixins (one per concern); `filter.py` ties them together
  (`__init__`, `Valves`, `inlet`, `outlet`).

### [x] 3.2 Single-file build step (OWUI constraint)
- `build.py` concatenates fragments in dependency order (header first, `Filter` last)
  → single `v1.6.1.py` for OWUI. Fragments share `_header.py` imports (no per-file
  imports to rewrite).
- **Verified:** built file is valid Python; all 67 method bodies byte-identical to
  the pre-split file; no methods missing/added/duplicated; all module-level
  constants preserved; clean MRO (mixins inherit `object` directly).

> **Dev workflow:** edit `src/*.py` → `python build.py` → paste `v1.6.1.py` into OWUI.
> Documented in README → "Development & Building".

---

## Phase 4 — Adaptive context window  ✅ DONE

Replaced the fixed/serialized threshold config with automatic per-model resolution.

### [x] 4.1 Valves
- **Removed** `compression_threshold_tokens` (absolute) and `model_thresholds`
  (per-model override string) — per-model is now automatic.
- **Added** `compression_threshold_percent` (int, default `80`): trigger at 80% of
  the active model's context window.
- **Kept** `max_context_tokens` (**repurposed**): fallback only, used when the model
  declares no `context_length`. `0` still means "no limit".

### [x] 4.2 Resolution helper (`src/compression.py`)
- Removed `_parse_model_thresholds` + `_get_model_thresholds` (and the
  `self._model_thresholds_cache`).
- Added:
  - `_extract_model_context_length(model_dict)` — reads `["info"]["meta"]`
    (filter `__model__` / `body["metadata"]["model"]`) and the flattened `["meta"]`
    (DB `model_dump`); rejects bool/str/≤0.
  - `_get_model_max_context(model_id, model_dict=None)` — priority:
    `__model__` meta → DB `Models.get_model_by_id(...).meta` → `max_context_tokens`.
  - `_get_compression_threshold(max_context)` — `percent%` of max context; `0` when
    `max_context <= 0` (compression skipped).

### [x] 4.3 Call sites
- `inlet`: debug log + 2 functional paths use `_get_model_max_context(model, __model__)`.
- `_check_and_generate_summary_async`: derives threshold from
  `body["metadata"]["model"]`; preflight `* 0.85` invariant and the
  `>= threshold` trigger preserved (now guarded by `threshold > 0`).
- `summarize.py` / `externalrefs.py` / `_get_summary_model_context_limit`: same helper.

### [x] 4.4 Verification
- `ast.parse` OK; no `self._*` calls unresolved; no stale
  `_get_model_thresholds` / `model_thresholds` references anywhere in `src/`.
- Pure-logic unit tests for the extractor (both dict shapes, type guards) and the
  percent math pass.
- README valve table + troubleshooting updated; adaptive-resolution section added.

> **Caveat:** `context_length` is a user-set extra key in OWUI's `ModelMeta`
> (`ConfigDict(extra='allow')`), so it is only present when the model declares it.
> OWUI core never populates it for any provider, so base models without metadata
> fall back to `max_context_tokens` — or, for llama.cpp, to the server probe (Phase 5).

---

## Phase 5 — llama.cpp context auto-detection  ✅ DONE

Because OWUI never populates `meta.context_length` for OpenAI-compatible
connections, ask the llama.cpp server directly.

### [x] 5.1 Valve
- Added `enable_llamacpp_context_probe` (bool, default `true`).

### [x] 5.2 Probes (`src/compression.py`)
- `_extract_llamacpp_n_ctx` — `GET /props` → `default_generation_settings.n_ctx`
  (runtime `--ctx-size`), also tolerating a top-level `n_ctx`.
- `_extract_llamacpp_n_ctx_train` — `GET /v1/models` → `data[].meta.n_ctx_train`
  (fallback), preferring the matching model id.
- `_llamacpp_root_url` — strips `/api/v1`, `/api/v0`, `/v1`.
- `_get_openai_connection_config` — resolves connection by `urlIdx` via the legacy
  `app.state.config` (OWUI ≤ 0.9.x) **or** the async `Config.get_many` API (OWUI ≥ 0.10).
- `_probe_llamacpp_context` — aiohttp GETs with a 5s timeout.
- `_get_llamacpp_context` — provider gate (`llama.cpp` or `owned_by=llamacpp`), custom
  models resolved through `info.base_model_id`, results cached (5 min hit / 1 min miss).

### [x] 5.3 Integration
- `_get_model_max_context` is now **async** and inserts the probe at step 3 (before the
  `max_context_tokens` fallback). `_get_summary_model_context_limit` is async too.
- All call sites (`inlet`, `_check_and_generate_summary_async`, `summarize.py`,
  `externalrefs.py`) now `await` the helpers.
- Removed the now-unused `_call_db_sync` from `src/db.py`.
- Added `aiohttp` + optional `OWUIConfig` imports to `src/_header.py`.

### [x] 5.4 Verification
- `ast.parse` OK; all helpers resolved & async; every call site awaited; no stale refs.
- Unit tests pass for the extractors (including bool/≤0 rejection), root-URL stripping,
  the probe paths (`/props` → `/v1/models` → `None`), and orchestration
  (provider gating, cache hit, base-model `urlIdx` resolution).
- `build.py` reproducible; README updated with the resolution step and a
  "llama.cpp auto-detection" section.

---

## Phase 6 — Memory tidy-up (per-chat maps)  ✅ DONE

Both per-chat state maps on the shared `Filter` singleton grew without bound.

### [x] 6.1 `_chat_locks` — forget the lock after the summary task
- `_locked_summary_task` now pops the chat's lock in a `finally` **while still
  holding it** (identity-checked via `self._chat_locks.get(chat_id) is lock`), so
  the map returns to empty once a chat is idle. The in-flight semantics are
  unchanged: concurrent outlets still see `lock.locked()` and skip, and a newer
  lock installed in the meantime is never removed by an older task's `finally`.

### [x] 6.2 `_pending_inlet_messages` — clear stale entries + hard cap
- `inlet` now pops any leftover entry for the chat right after resolving
  `chat_id` (covers a previous turn whose outlet never ran, e.g. Stop/abort).
- `_capture_pending_inlet_messages` evicts oldest chats beyond
  `PENDING_INLET_MAX_CHATS = 64` (new constant in `src/_header.py`), bounding
  memory even for never-revisited chats.

### [x] 6.3 Verification
- `ast.parse` OK; `build.py` reproducible (SHA-256 identical across runs).
- Unit tests on the extracted methods: lock forgotten after completion AND on
  exception; mid-flight lock shared for the skip-check; identity guard keeps a
  newer lock; cap eviction is oldest-first; empty capture pops the entry.

---

## Notes / open questions
- [x] Cross-chat references? → **YES, in use** (keep 2.1)
- [x] Native function calling? → **YES, in use** (keep 2.2)
- [x] Keep i18n at 2 locales or go en-US-only? (1.3) — defaulting to en-US + zh-CN
