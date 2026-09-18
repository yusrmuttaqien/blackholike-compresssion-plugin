# v1.6.1 Lean Pass — Ordered Plan

Goal: make the code as lean as possible. **Remove first, then dedup, then micro-fix.**
Baseline: `v1.6.1.py` = **4,036 lines** (post-split, byte-identical to `src/` build).
Source of truth for findings: `assessment.md` (v2). Flow reference: `flows.md`.

> Supersedes the earlier v1.6.1 refactor plan (4,813 → 3,932 lines, split into
> `src/` fragments + `build.py`), which is complete.

**Dev workflow:** edit `src/*.py` → `python build.py` → verify → `v1.6.1.py`.

---

## Phase 1 — Pure deletions (zero behavior change)

### [x] 1.1 Item 1: delete dead `_generate_referenced_summaries_background` (~92 lines) ✅
- `src/externalrefs.py:259` — never called anywhere.

### [x] 1.2 Item 2: delete unused `import traceback` ✅
- `src/summarize.py:447` — imported in exception handler, never used.

### [x] 1.3 Item 3: drop redundant `import json as json_module` ✅
- `src/summarize.py:762` — module-level `import json` already exists; use `json.loads`.

### [x] 1.4 Item 4: remove unused `message_id` from `_get_chat_context` ✅
- `src/compression.py:424` — both callers only read `chat_id`.

### [x] 1.5 Item 5: remove unused `user_id`/`user_name` from `_get_user_context` ✅
- `src/compression.py:265` — callers only use `user_language`.

### [x] 1.6 Item 6: drop unused `body` param from `_load_summary` ✅
- `src/db.py:348` + call site in `src/summarize.py`.

### [x] 1.7 Item 7: drop unused `__model__` param from `_should_skip_compression` ✅
- `src/compression.py:460` + call sites in `src/filter.py` (inlet, outlet).
- Kept `__model__` in inlet/outlet signatures — part of the OWUI filter API.

### [x] 1.8 Item 8: fix dead conditional in `_truncate_messages_for_summary` ✅
- `src/summarize.py:451` — inlined the constant string; `msg_name` removed.

### [x] 1.9 Item 20: drop dead `debug_stats`/`collect_debug` machinery ✅
- `src/toolcalls.py:63` — now returns only `trimmed_count`; inlet logs it under
  the debug-log gate ("Tool trimming: N tool output(s) trimmed").

### [x] 1.10 Item 21: delete dead `message_source`/`restored_count_before` in outlet ✅
- `src/filter.py:885–909`.

### [x] 1.11 Item 14: delete `v1.6.1.py.bak` (211 KB) ✅

### [x] 1.12 Item 15: add `.gitignore` ✅

> **Phase 1 complete: 4,036 → 3,860 lines (−176).** Verified: `python3 build.py`
> OK, `py_compile` OK, method-set diff = exactly
> `_generate_referenced_summaries_background` removed / 0 added, no stale
> refs (`debug_stats`, `message_source`, `json_module`, etc.) in the built file.
> One new debug log line added in inlet (trimmed count) — the only behavior
> delta, and it only fires with `show_debug_log` on.

---

## Phase 2 — Dedup refactors (behavior-preserving, ~150 lines)

- [x] Item 9: `_emit_context_usage_status(tokens, max_tokens, lang, emitter)` helper in
      `src/console.py` — all 4 call sites converted (`src/compression.py`,
      `src/summarize.py`, `src/filter.py` ×2).
- [x] Item 10: `_resolve_context_tokens(messages, limit)` in `src/tokens.py` returns
      `(total, estimated, used_precise)` — 3 call sites converted; also replaced the
      fragile `total_tokens == estimated_tokens` equality checks with `used_precise`.
- [x] Item 11: `_extract_system_from_params(params)` in `src/compression.py` — both
      call sites converted (`src/filter.py`, `src/summarize.py`).
- [x] Item 12: `_drop_oldest_atomic_group(...)` in `src/toolcalls.py` — both inlet
      loops converted (`preserve_protected=False/True` covers the system/external-ref
      protection difference).
- [x] Item 13: `target_compressed_count` — **kept** (O(n) index walk, feeds one useful
      boundary log line on every inlet request).

> **Phase 2 complete: 3,860 → 3,794 lines (−66; ~150 lines of duplication removed,
> offset by 4 new helpers).** Verified: build + py_compile OK, method-set diff =
> exactly 4 helpers added / 0 removed, all `self._*` calls resolve, full line-by-line
> diff reviewed. One cleanup bonus: `used_precise` flag replaces the
> `total == estimated` coincidence checks in the drop loops and stats block.

---

## Phase 3 — Micro-fixes (behavior-preserving)

- [x] Item 16: `_get_db_sync_pool()` — lazily created shared single-worker pool
      (`thread_name_prefix="owui-db"`); `_call_db_sync` no longer creates a pool
      per call (`src/db.py`).
- [x] Item 17: bounded maps — `_get_chat_lock` drops idle locks once
      `_chat_locks` reaches 128; `_capture_pending_inlet_messages` evicts the
      oldest chat's pending messages once the map exceeds 64 (`src/compression.py`).
- [x] Item 18: `_calculate_messages_tokens(next_context)` now runs via
      `asyncio.to_thread` in the summary status path (`src/summarize.py`).
- [x] Item 19: `self._frontend_broadcast_broken` flag (initialized in
      `Filter.__init__`) replaces the runtime mutation of the
      `show_debug_log` valve; gate moved to the emit choke point in
      `_emit_frontend_console_log` (`src/console.py`, `src/filter.py`).

> **Phase 3 complete: 3,794 → 3,825 lines (+31 — defensive code added on purpose:**
> shared-pool helper, bound checks, broadcast flag).** Verified: build +
> py_compile OK, diff = exactly the 4 fixes, no stale patterns.
> Net across phases 1–3: 4,036 → 3,825 lines (−211).

---

## Phase 4 — Structural splits (readability, behavior-preserving)

`inlet` (627 lines) and `_generate_summary_async` (408 lines) were the two
flow-following bottlenecks. Split into verbatim-extracted helpers:

- [x] 4.1 `inlet` → 3 helpers in `src/filter.py`:
      - `_extract_inlet_system_prompt(body, messages, __event_call__)` — DB →
        messages system-prompt extraction + message-role stats log.
      - `_assemble_summary_view(...)` — the with-summary branch
        ([Head] + [Summary Message] + [Tail], budget fit, stats, status).
      - `_assemble_plain_view(...)` — the no-summary branch (external-ref
        injection, budget fit, status); returns `None` for empty history.
      `inlet` is now ~210 lines and reads top-to-bottom.
- [x] 4.2 `_generate_summary_async` → 3 helpers in `src/summarize.py`:
      - `_resolve_summary_boundary(messages, target, __event_call__)` —
        visible-range resolution for the next compression boundary.
      - `_fit_summary_request(body, chat_id, summary_index, middle, protected_prefix, ...)`
        — model/limits resolution, previous-summary load, atomic-shrink loop.
      - `_emit_post_summary_usage_status(...)` — post-save next-context
        token recompute + usage status.
- [x] 4.3 Removed the duplicate `# Try to get from DB (custom model)` comment
      left over in `src/filter.py` (Phase 1 leftover).
- [x] 4.4 Docs updated: `flows.md` rewritten line-number-free (method + fragment
      references), reflecting Phases 1–4; `assessment.md` update log v3.
- [x] 4.5 Bugfix: re-add `external_refs_injected_count = 0` at the top of
      `_assemble_summary_view` and `_assemble_plain_view`. The original
      initializer lived in `inlet` *before* the branch and was deleted with
      it, but the branches only assigned the variable conditionally (when
      external refs exist) — so the common no-refs path raised
      `UnboundLocalError`. Verified with a stubbed-import smoke test driving
      both helpers' no-refs path.
- [x] 4.6 Distinguish the outlet status reading from the inlet's. The inlet
      emissions (with-summary + no-summary branches) and the post-summary
      emission all count the *full request* (system prompt + history), but
      the outlet background check counts the saved conversation history only
      (Open WebUI's outlet body never contains the model's system prompt), so
      the two numbers per turn looked contradictory (e.g. 1552 → 8). Added
      i18n key `status_history_usage` (en-US: "History Usage (Estimated): …",
      zh-CN: "对话历史用量 (预估): …"), a `label_key` param on
      `_emit_context_usage_status`, and the outlet site
      (src/compression.py) now passes `label_key="status_history_usage"`.
      The other three sites keep the default `status_context_usage` label.
      Also added i18n key `status_compaction_drives` (en-US: " | (drives
      compaction)", zh-CN: " | (触发压缩)") plus a `note_key` param on
      `_emit_context_usage_status`; the outlet site passes
      `note_key="status_compaction_drives"` to make explicit that the History
      Usage reading — not the inlet's Context Usage reading — is the one the
      compaction threshold checks against. Verified: build + py_compile OK,
      exactly one call site uses the new keys.

> **Phase 4 complete: 3,825 → 4,023 lines (+198 — helper signatures, docstrings,
> and multi-line call wrapping; no logic added).** Verified: build + py_compile
> OK, method-set diff = exactly 6 helpers added / 0 removed, every helper body
> verified byte-identical to the original inline code (modulo indentation and
> the intentional `return body` → `return None, ...` / bare-`return` →
> `return None` conversions), all `self._*` calls resolve, one call site per
> helper in the built file.
>
> **Net across all four phases: 4,036 → 4,027 lines (−9), plus the two largest
> functions reduced from 627/408 lines to 210/178 lines of orchestration.**
>
> Note: 4.5 (initializers) added 4 lines. Full `inlet` smoke test (stubbed
> open_webui/fastapi/DB, no-summary and with-summary branches) passes:
> no-summary returns messages as-is; with-summary injects the summary as
> the first assistant message.

---

## Notes
- Every item cross-referenced to `assessment.md` item numbers.
- Each phase ends with: rebuild + py_compile + diff review of the built file.
