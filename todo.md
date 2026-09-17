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

## Notes / open questions
- [x] Cross-chat references? → **YES, in use** (keep 2.1)
- [x] Native function calling? → **YES, in use** (keep 2.2)
- [ ] Keep i18n at 2 locales or go en-US-only? (1.3) — defaulting to en-US + zh-CN
