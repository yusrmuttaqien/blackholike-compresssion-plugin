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

- [ ] Item 9: `_emit_usage_status(tokens, max_tokens, lang, emitter)` helper — 4 call sites
      (`src/compression.py`, `src/summarize.py`, `src/filter.py` ×2).
- [ ] Item 10: shared "estimate → precise count if within 15% of limit" helper — 3 call sites.
- [ ] Item 11: shared "system prompt from model params" helper — 2 call sites
      (`src/filter.py`, `src/summarize.py`).
- [ ] Item 12: merge the two drop-oldest-atomic-groups loops in `inlet`
      (`src/filter.py:498,720`).
- [ ] Item 13: decide `inlet`'s `target_compressed_count` — keep (single log line)
      or drop the computation.

---

## Phase 3 — Micro-fixes (behavior-preserving)

- [ ] Item 16: shared module-level thread pool in `_call_db_sync` (`src/db.py:105`).
- [ ] Item 17: bound `_chat_locks` and `_pending_inlet_messages`
      (evict when idle/completed) — unbounded per-chat growth.
- [ ] Item 18: wrap `_calculate_messages_tokens(next_context)` in
      `asyncio.to_thread` for consistency (`src/summarize.py:379`).
- [ ] Item 19: replace runtime mutation of `self.valves.show_debug_log`
      with a proper runtime flag (`src/console.py:90`).

---

## Notes
- Every item cross-referenced to `assessment.md` item numbers.
- Each phase ends with: rebuild + py_compile + diff review of the built file.
