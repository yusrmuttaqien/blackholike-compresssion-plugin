# ── summarize.py · Summary prompt building + LLM call ─────────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


class SummarizeMixin:

    """Summary prompt building + LLM call."""

    def _clean_model_id(self, model_id: Optional[str]) -> Optional[str]:
        """Cleans the model ID by removing whitespace and quotes."""
        if not model_id:
            return None
        cleaned = model_id.strip().strip('"').strip("'")
        return cleaned if cleaned else None

    async def _generate_summary_async(
        self,
        messages: list,
        chat_id: str,
        body: dict,
        user_data: Optional[dict],
        target_compressed_count: Optional[int],
        lang: str = "en-US",
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __event_call__: Callable[[Any], Awaitable[None]] = None,
        __request__: Request = None,
    ):
        """
        Generates summary asynchronously (runs in background, does not block response).
        Logic:
        1. Extract the visible message slice that maps to the next original-history boundary.
        2. If the summary model window is smaller than that slice, keep the oldest slice and trim the newest atomic groups.
        3. Generate summary for the remaining messages and save the exact covered boundary.
        """
        try:
            await self._log(
                f"\n[🤖 Async Summary Task] Starting...", event_call=__event_call__
            )

            # 1. Get target compression progress in original-history coordinates.
            if target_compressed_count is None:
                target_compressed_count = self._calculate_target_compressed_count(
                    messages
                )
                await self._log(
                    f"[🤖 Async Summary Task] ⚠️ target_compressed_count is None, estimating: {target_compressed_count}",
                    log_type="warning",
                    event_call=__event_call__,
                )

            # 2. Determine the visible message range that maps to the target original
            # compression progress.
            summary_state = self._get_summary_view_state(messages)
            summary_index = summary_state["summary_index"]
            base_progress = summary_state["base_progress"] or 0

            if summary_index is None:
                start_index = self._get_effective_keep_first(messages)
                end_index = min(len(messages), target_compressed_count)
                protected_prefix = 0
            else:
                start_index = summary_index
                end_index = min(
                    len(messages),
                    summary_index + 1 + max(0, target_compressed_count - base_progress),
                )
                protected_prefix = 1

            # Ensure indices are valid
            if start_index >= end_index:
                await self._log(
                    f"[🤖 Async Summary Task] Middle messages empty (Start: {start_index}, End: {end_index}), skipping\n"
                    f"  summary_index={summary_index} | base_progress={base_progress} | "
                    f"target_compressed_count={target_compressed_count} | "
                    f"keep_first={self.valves.keep_first} | keep_last={self.valves.keep_last} | "
                    f"total_messages={len(messages)}",
                    log_type="warning",
                    event_call=__event_call__,
                )
                return

            middle_messages = messages[start_index:end_index]
            tail_preview_msgs = messages[end_index:]

            if self.valves.show_debug_log and __event_call__:
                middle_preview = [
                    f"{i + start_index}: [{m.get('role')}] {m.get('content', '')[:20]}..."
                    for i, m in enumerate(middle_messages[:3])
                ]
                tail_preview = [
                    f"{i + end_index}: [{m.get('role')}] {m.get('content', '')[:20]}..."
                    for i, m in enumerate(tail_preview_msgs)
                ]
                await self._log(
                    f"[🤖 Async Summary Task] 📊 Boundary Check:\n"
                    f"  - Middle (Compressing): {len(middle_messages)} msgs (Indices {start_index}-{end_index-1}) -> Preview: {middle_preview}\n"
                    f"  - Tail (Keeping): {len(tail_preview_msgs)} msgs (Indices {end_index}-End) -> Preview: {tail_preview}",
                    event_call=__event_call__,
                )

            # 3. Check Token limit and truncate (Max Context Truncation)
            # [Optimization] Use the summary model's (if any) threshold to decide how many middle messages can be processed
            # This allows using a long-window model (like gemini-flash) to compress history exceeding the current model's window
            summary_model_id = self._clean_model_id(
                self.valves.summary_model
            ) or self._clean_model_id(body.get("model"))

            if not summary_model_id:
                await self._log(
                    "[🤖 Async Summary Task] ⚠️ Summary model does not exist, skipping compression",
                    log_type="warning",
                    event_call=__event_call__,
                )
                return

            max_context_tokens = self._get_summary_model_context_limit(summary_model_id)
            request_limits = self._compute_summary_request_limits(max_context_tokens)

            await self._log(
                f"[🤖 Async Summary Task] Using max limit for model {summary_model_id}: {max_context_tokens} Tokens",
                event_call=__event_call__,
            )
            if max_context_tokens > 0:
                await self._log(
                    "[🤖 Async Summary Task] Summary request budget: "
                    f"input<={request_limits['max_input_tokens']}t | "
                    f"output<={request_limits['max_output_tokens']}t | "
                    f"safety={request_limits['safety_margin_tokens']}t",
                    event_call=__event_call__,
                )

            # Determine previous_summary to pass to LLM before final prompt budgeting.
            # When summary_index is not None, the old summary message is already the first
            # entry of middle_messages (protected_prefix=1), so it appears verbatim in
            # conversation_text — no need to inject separately.
            # When summary_index is None the outlet messages come from raw DB history that
            # has never had the summary injected, so we must load it from DB explicitly.
            if summary_index is None:
                previous_summary = await self._load_summary(chat_id, body)
                if previous_summary:
                    await self._log(
                        "[🤖 Async Summary Task] Loaded previous summary from DB to pass as context (summary not in messages)",
                        event_call=__event_call__,
                    )
            else:
                previous_summary = None

            if max_context_tokens <= 0:
                await self._log(
                    "[🤖 Async Summary Task] No max_context_tokens limit set (0). Skipping final request budgeting.",
                    event_call=__event_call__,
                )
            # Fit the exact final request prompt, not just middle-message heuristics.
            prompt_tokens = 0
            while max_context_tokens > 0:
                if not middle_messages:
                    await self._log(
                        "[🤖 Async Summary Task] Middle messages empty after final request shrink, skipping summary generation",
                        event_call=__event_call__,
                    )
                    return

                conversation_text = self._format_messages_for_summary(middle_messages)
                summary_prompt = self._build_summary_prompt(
                    conversation_text, previous_summary=previous_summary
                )
                prompt_tokens = await asyncio.to_thread(
                    self._count_tokens, summary_prompt
                )

                if prompt_tokens <= request_limits["max_input_tokens"]:
                    break

                overflow_tokens = prompt_tokens - request_limits["max_input_tokens"]
                await self._log(
                    f"[🤖 Async Summary Task] ⚠️ Final summary request input ({prompt_tokens} Tokens) exceeds safe budget ({request_limits['max_input_tokens']}), shrinking by at least {overflow_tokens} Tokens",
                    log_type="warning",
                    event_call=__event_call__,
                )

                trimmable_middle = middle_messages[protected_prefix:]
                summary_atomic_groups = self._get_atomic_groups(trimmable_middle)
                if len(summary_atomic_groups) > 1:
                    group_indices = summary_atomic_groups.pop()
                    removed_count = len(group_indices)
                    removed_preview_tokens = sum(
                        self._estimate_content_tokens(
                            trimmable_middle[-offset].get("content", "")
                        )
                        for offset in range(1, removed_count + 1)
                    )
                    for _ in range(removed_count):
                        trimmable_middle.pop()
                    middle_messages = (
                        middle_messages[:protected_prefix] + trimmable_middle
                    )
                    await self._log(
                        f"[🤖 Async Summary Task] Removed newest atomic group ({removed_count} msgs, est {removed_preview_tokens} Tokens) to fit final request payload",
                        event_call=__event_call__,
                    )
                    continue

                if protected_prefix > 0:
                    middle_messages = middle_messages[1:]
                    protected_prefix = 0
                    await self._log(
                        "[🤖 Async Summary Task] Dropped embedded previous summary marker from compression input to fit final request payload",
                        log_type="warning",
                        event_call=__event_call__,
                    )
                    continue

                if previous_summary:
                    previous_summary = None
                    await self._log(
                        "[🤖 Async Summary Task] Dropped DB-backed previous summary from prompt to fit final request payload",
                        log_type="warning",
                        event_call=__event_call__,
                    )
                    continue

                await self._log(
                    "[🤖 Async Summary Task] Unable to fit final summary request within model budget after shrinking. Skipping summary generation.",
                    log_type="error",
                    event_call=__event_call__,
                )
                return

            if not middle_messages:
                await self._log(
                    "[🤖 Async Summary Task] Middle messages empty after truncation, skipping summary generation",
                    event_call=__event_call__,
                )
                return

            # 4. Build conversation text using the fitted request payload.
            conversation_text = self._format_messages_for_summary(middle_messages)
            if max_context_tokens > 0:
                await self._log(
                    f"[🤖 Async Summary Task] Final fitted summary input: {prompt_tokens} / {request_limits['max_input_tokens']} Tokens",
                    event_call=__event_call__,
                )

            # 6. Call LLM to generate new summary

            # Send status notification for starting summary generation
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": self._get_translation(
                                lang, "status_generating_summary"
                            ),
                            "done": False,
                        },
                    }
                )

            new_summary = await self._call_summary_llm(
                conversation_text,
                {**body, "model": summary_model_id},
                user_data,
                __event_call__,
                __request__,
                previous_summary=previous_summary,
            )

            if not new_summary:
                await self._log(
                    "[🤖 Async Summary Task] ⚠️ Summary generation returned empty result, skipping save",
                    log_type="warning",
                    event_call=__event_call__,
                )
                return

            if summary_index is None:
                saved_compressed_count = start_index + len(middle_messages)
            else:
                saved_compressed_count = base_progress + max(
                    0, len(middle_messages) - protected_prefix
                )

            # 6. Save new summary
            await self._log(
                "[Optimization] Saving summary in a background thread to avoid blocking the event loop.",
                event_call=__event_call__,
            )

            await self._save_summary(chat_id, new_summary, saved_compressed_count)

            # Send completion status notification
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": self._get_translation(
                                lang,
                                "status_loaded_summary",
                                count=len(middle_messages),
                            ),
                            "done": True,
                        },
                    }
                )

            await self._log(
                f"[🤖 Async Summary Task] ✅ Complete! New summary length: {len(new_summary)} characters",
                log_type="success",
                event_call=__event_call__,
            )
            await self._log(
                f"[🤖 Async Summary Task] Progress update: Compressed up to original message {saved_compressed_count}",
                event_call=__event_call__,
            )

            # --- Token Usage Status Notification ---
            if self.valves.show_token_usage_status and __event_emitter__:
                try:
                    # 1. Fetch System Prompt (DB fallback)
                    system_prompt_msg = None
                    model_id = body.get("model")
                    if model_id:
                        try:
                            model_obj = await _call_db(Models.get_model_by_id, model_id)
                            if model_obj and model_obj.params:
                                params = model_obj.params
                                if isinstance(params, str):
                                    params = json.loads(params)
                                if isinstance(params, dict):
                                    sys_content = params.get("system")
                                else:
                                    sys_content = getattr(params, "system", None)

                                if sys_content:
                                    system_prompt_msg = {
                                        "role": "system",
                                        "content": sys_content,
                                    }
                        except Exception:
                            pass  # Ignore DB errors here, best effort

                    # 2. Construct Next Context using the saved original-history boundary.
                    next_summary_msg = self._build_summary_message(
                        new_summary, lang, saved_compressed_count
                    )
                    if summary_index is None:
                        effective_keep_first = self._get_effective_keep_first(messages)
                        head_msgs = (
                            messages[:effective_keep_first]
                            if effective_keep_first > 0
                            else []
                        )
                        visible_tail_start = max(
                            saved_compressed_count, effective_keep_first
                        )
                    else:
                        head_msgs = messages[:summary_index]
                        visible_tail_start = (
                            summary_index
                            + 1
                            + max(0, saved_compressed_count - base_progress)
                        )

                    tail_msgs = messages[visible_tail_start:]

                    # Assemble
                    next_context = head_msgs + [next_summary_msg] + tail_msgs

                    # Inject system prompt if needed
                    if system_prompt_msg:
                        is_in_head = any(m.get("role") == "system" for m in head_msgs)
                        if not is_in_head:
                            next_context = [system_prompt_msg] + next_context

                    # 4. Calculate Tokens
                    token_count = self._calculate_messages_tokens(next_context)

                    # 5. Get Thresholds & Calculate Ratio
                    model = self._clean_model_id(body.get("model"))
                    thresholds = self._get_model_thresholds(model)
                    max_context_tokens = thresholds.get(
                        "max_context_tokens", self.valves.max_context_tokens
                    )
                    # 6. Emit Status (only if threshold is met)
                    if max_context_tokens > 0:
                        usage_ratio = token_count / max_context_tokens
                        # Only show status if threshold is met
                        if self._should_show_status(usage_ratio):
                            status_msg = self._get_translation(
                                lang,
                                "status_context_usage",
                                tokens=token_count,
                                max_tokens=max_context_tokens,
                                ratio=f"{usage_ratio*100:.1f}",
                            )
                            if usage_ratio > 0.9:
                                status_msg += self._get_translation(
                                    lang, "status_high_usage"
                                )

                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": status_msg,
                                        "done": True,
                                    },
                                }
                            )
                except Exception as e:
                    await self._log(
                        f"[Status] Error calculating tokens: {e}",
                        log_type="error",
                        event_call=__event_call__,
                    )

        except Exception as e:
            await self._log(
                f"[🤖 Async Summary Task] ❌ Error: {str(e)}",
                log_type="error",
                event_call=None,
            )
            if __event_call__ and not getattr(e, "_frontend_logged", False):
                await self._emit_frontend_console_log(
                    f"[🤖 Async Summary Task] ❌ Error: {str(e)}",
                    log_type="error",
                    event_call=__event_call__,
                    force=True,
                )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": self._get_translation(
                                lang, "status_summary_error", error=str(e)[:100]
                            ),
                            "done": True,
                        },
                    }
                )

            import traceback

            logger.exception("[🤖 Async Summary Task] Unhandled exception")

    def _truncate_messages_for_summary(self, messages: list, max_tokens: int) -> str:
        formatted = []
        total_tokens = 0

        for msg in reversed(messages):
            role = msg.get("role", "unknown")
            content = self._extract_text_content(msg.get("content", ""))
            msg_id = msg.get("id", "N/A")
            msg_name = msg.get("name", "")

            name_part = f" [ID: {msg_id}]" if msg_name else f" [ID: {msg_id}]"
            formatted_msg = f"#### {role.capitalize()}{name_part}\n{content}\n"
            formatted_msg_tokens = _estimate_text_tokens(formatted_msg)

            if total_tokens + formatted_msg_tokens > max_tokens:
                break
            formatted.append(formatted_msg)
            total_tokens += formatted_msg_tokens

        formatted.reverse()
        return "\n".join(formatted)

    def _compute_summary_request_limits(
        self, max_context_tokens: int
    ) -> Dict[str, int]:
        """Reserve conservative input/output budgets for summary requests."""
        desired_output_tokens = max(1, int(self.valves.max_summary_tokens or 1))
        if max_context_tokens <= 0:
            return {
                "max_context_tokens": 0,
                "max_input_tokens": 0,
                "max_output_tokens": desired_output_tokens,
                "safety_margin_tokens": 0,
            }

        safety_margin_tokens = min(4096, max(512, max_context_tokens // 20))
        available_after_margin = max(512, max_context_tokens - safety_margin_tokens)
        max_output_tokens = min(
            desired_output_tokens, max(512, available_after_margin // 3)
        )
        max_input_tokens = max(
            256, max_context_tokens - max_output_tokens - safety_margin_tokens
        )

        return {
            "max_context_tokens": max_context_tokens,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "safety_margin_tokens": safety_margin_tokens,
        }

    def _format_messages_for_summary(self, messages: list) -> str:
        """
        Formats messages for summarization with metadata awareness.
        Preserves IDs, names, and key metadata fragments to ensure traceability.
        """
        formatted = []
        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "unknown")
            content = self._extract_text_content(msg.get("content", ""))

            # Extract Identity Metadata
            msg_id = msg.get("id", "N/A")
            msg_name = msg.get("name", "")
            # Only pick non-system, interesting metadata keys
            metadata = msg.get("metadata", {})
            safe_meta = {
                k: v
                for k, v in metadata.items()
                if k not in ["is_trimmed", "is_summary"]
            }

            # Handle role name
            role_name = {"user": "User", "assistant": "Assistant"}.get(role, role)

            meta_str = f" [ID: {msg_id}]"
            if msg_name:
                meta_str += f" [Name: {msg_name}]"
            if safe_meta:
                meta_str += f" [Meta: {safe_meta}]"

            formatted.append(f"[{i}] {role_name}{meta_str}: {content}")

        return "\n\n".join(formatted)

    def _build_summary_prompt(
        self,
        new_conversation_text: str,
        previous_summary: Optional[str] = None,
    ) -> str:
        """Build the exact summary prompt sent to the LLM."""
        previous_summary_block = (
            f"<previous_working_memory>\n{previous_summary}\n</previous_working_memory>\n\n"
            if previous_summary
            else ""
        )
        return f"""You are an expert Conversation-and-Tool-State Compression Engine. Produce a compact, low-loss working memory for continuing a multi-turn chat that may include repeated tool usage, external references, and partial/trimmed content.

### Primary Objective
Preserve the information that most helps the NEXT response stay accurate, consistent, and context-aware. Prefer continuity over maximal compression. Do not drop useful state just because it is old or already resolved if it still changes what the assistant should remember.

### Processing Rules
1. **State-Aware Merging**: If `<previous_working_memory>` exists, merge it with the new input. Preserve still-valid facts; update changed state; remove only information that is clearly obsolete and no longer useful.
2. **User Intent First**: Preserve the current user goal, explicit preferences, constraints, dislikes, decisions, and acceptance criteria before less important detail.
3. **Persistent Context Gate**: Put something in `<persistent_context>` ONLY if it is likely to remain useful across future turns beyond the immediate task, such as stable user preferences, lasting project facts, standing constraints, chosen defaults, or durable decisions. Do NOT store short-term topics, one-off questions, or speculative user traits there.
4. **Recent Progress Gate**: Put current-task developments, recent requests, intermediate conclusions, and short-lived context into `<recent_progress>` instead of `<persistent_context>`, even if they seem important right now.
5. **Tool-State Compression**: For tool calls, keep only durable state: tool name, purpose, decisive inputs when important, verified outputs, key metrics, errors, root causes, and what remains unfinished. Discard raw boilerplate and long low-value payloads.
6. **Error Preservation**: Preserve important error messages, exception names, exit codes, and last useful stack/location details when they help future turns, even if partially resolved.
7. **External Reference Handling**: If `<external_references>` or `<referenced_chats>` blocks appear, treat them as supplemental context sources. Keep only durable facts, prior decisions, or constraints relevant to the current thread. Do not mention wrappers or that references were injected.
8. **Trim-Aware Discipline**: If content is marked trimmed/collapsed, treat omitted portions as unknown. Keep visible facts only; never infer hidden details.
9. **Verification Discipline**: Distinguish verified facts, inferred conclusions, and open questions. Do not silently upgrade uncertainty into fact. If a point is not explicitly established, label it `INFERRED:` or `OPEN:`.
10. **Verbatim Retention**: Preserve exact code, commands, file paths, config values, numbers, and short error strings character-for-character when they matter.
11. **Denoising**: Remove greetings, repetition, filler, UI/debug wrappers, and placeholder text such as "[Content collapsed]" unless the placeholder itself is materially relevant.
12. **Summary Hygiene**: Absorb prior summary content semantically, but do not echo prompt wrappers, XML input tags, or old summary boilerplate into the new result.
13. **Continuity Anchors**: Preserve created deliverables, published links, message IDs, explicit decisions, chosen defaults, and pending asks whenever they could affect the next turn.
14. **Preference Evidence Rule**: Only record user preferences when they are explicitly stated, repeatedly demonstrated, or materially affect future replies. Do not infer durable preferences from a single request.
15. **Loss-Minimization Bias**: When unsure whether a fact may matter later, keep a short factual note instead of deleting it, but place it in the least-strong section that fits.
16. **General-Chat Coverage**: Even without tools or code, preserve unanswered questions, promised follow-ups, personal constraints, and conversation commitments that the next reply should honor.

### Output Constraints
* **Format**: Output XML only. No markdown. No prose outside the XML root.
* **Token Budget**: Stay under {self.valves.max_summary_tokens} tokens. Trim low-value detail before high-value state.
* **Language**: Match the dominant conversation language.
* **Style**: Dense, factual, low-fluff, easy for another assistant turn to consume.
* **Empty Sections**: Omit empty sections entirely.

### Output Schema
Use this exact top-level structure:

<working_memory>
  <current_goal>...</current_goal>
  <user_preferences>
    <item>...</item>
  </user_preferences>
  <persistent_context>
    <item>...</item>
  </persistent_context>
  <recent_progress>
    <item>...</item>
  </recent_progress>
  <tool_state>
    <item>tool=... | purpose=... | result=... | status=... | next=...</item>
  </tool_state>
  <errors_and_warnings>
    <item>...</item>
  </errors_and_warnings>
  <open_loops>
    <item>...</item>
  </open_loops>
  <next_reply_guidance>
    <item>...</item>
  </next_reply_guidance>
</working_memory>

### Output Notes
* Use `<item>` entries for lists.
* When useful, include `[ID: ...]` inline inside text.
* For inferred or uncertain items, begin the item text with `INFERRED:` or `OPEN:`.
* `<current_goal>` should describe the latest actionable objective, not a broad theme.
* `<user_preferences>` should contain only explicit or high-confidence recurring preferences that affect response style or decisions.
* `<persistent_context>` should be sparse and conservative. When unsure, place the item in `<recent_progress>` instead.
* `<tool_state>` should focus on active tools, recent decisive results, and unfinished follow-ups. Omit stale one-off tool history.
* `<open_loops>` should contain only real unresolved tasks, blockers, or unanswered questions. Do not invent speculative future work just to fill the section.
* Do not create personality traits, long-term interests, or durable preferences unless the conversation explicitly supports them.
* Keep the output useful for both general conversation continuity and multi-step tool workflows.

<compression_input>
{previous_summary_block}<conversation>
{new_conversation_text}
</conversation>
</compression_input>

Return only the XML working memory:
"""

    def _extract_provider_error(self, response: Any) -> Optional[str]:
        """Extract upstream provider error details from non-standard response dicts."""
        if not isinstance(response, dict):
            return None

        if isinstance(response.get("choices"), list) and response.get("choices"):
            return None

        if "error" in response:
            error = response.get("error")
            if isinstance(error, dict):
                parts = []
                for key in ("message", "type", "code", "status", "detail"):
                    value = error.get(key)
                    if value in (None, "", []):
                        continue
                    parts.append(str(value) if key == "message" else f"{key}={value}")
                return "; ".join(parts) or json.dumps(error, ensure_ascii=False)
            if error not in (None, "", []):
                return str(error)

        for key in ("detail", "message", "error_message", "error_msg"):
            value = response.get(key)
            if value in (None, "", []):
                continue
            if isinstance(value, dict):
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        return None

    def _get_summary_chat_template_kwargs(self) -> dict:
        """
        Parse the summary_chat_template_kwargs valve (a JSON string) into a dict.

        Returns {} when the valve is empty or invalid, so a misconfigured valve
        degrades to 'no template kwargs' instead of breaking the LLM call.
        """
        raw = self.valves.summary_chat_template_kwargs
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning(
                "summary_chat_template_kwargs is not valid JSON; ignoring: %r", raw
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def _call_summary_llm(
        self,
        new_conversation_text: str,
        body: dict,
        user_data: dict,
        __event_call__: Callable[[Any], Awaitable[None]] = None,
        __request__: Request = None,
        previous_summary: Optional[str] = None,
    ) -> str:
        """
        Calls the LLM to generate a summary using Open WebUI's built-in method.
        """
        await self._log(
            f"[🤖 LLM Call] Using Open WebUI's built-in method",
            event_call=__event_call__,
        )

        summary_prompt = self._build_summary_prompt(
            new_conversation_text, previous_summary=previous_summary
        )
        # Determine the model to use
        model = self._clean_model_id(self.valves.summary_model) or self._clean_model_id(
            body.get("model")
        )

        if not model:
            await self._log(
                "[🤖 LLM Call] ⚠️ Summary model does not exist, skipping summary generation",
                log_type="warning",
                event_call=__event_call__,
            )
            return ""

        await self._log(f"[🤖 LLM Call] Model: {model}", event_call=__event_call__)

        max_context_tokens = self._get_summary_model_context_limit(model)
        request_limits = self._compute_summary_request_limits(max_context_tokens)
        max_output_tokens = request_limits["max_output_tokens"]

        # Build payload
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": summary_prompt}],
            "stream": False,
            "max_tokens": max_output_tokens,
            "temperature": self.valves.summary_temperature,
        }
        # Forward chat-template variables (e.g. {"enable_thinking": false}) so
        # OpenAI-compatible backends that honor chat_template_kwargs can turn
        # thinking off for the summary call. Omitted when empty.
        chat_template_kwargs = self._get_summary_chat_template_kwargs()
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs

        try:
            # Get user object
            user_id = user_data.get("id") if user_data else None
            if not user_id:
                raise ValueError("Could not get user ID")

            # [Optimization] Users.get_user_by_id is now async in OpenWebUI 0.9.x.
            await self._log(
                "[Optimization] Getting user object via async DB call.",
                event_call=__event_call__,
            )
            user = await _call_db(Users.get_user_by_id, user_id)

            if not user:
                raise ValueError(f"Could not find user: {user_id}")

            await self._log(
                f"[🤖 LLM Call] User: {user.email}\n[🤖 LLM Call] Sending request...",
                event_call=__event_call__,
            )

            # Use the injected request if available, otherwise fall back to a minimal synthetic one
            request = __request__ or Request(scope={"type": "http", "app": webui_app})

            # Call generate_chat_completion
            response = await generate_chat_completion(request, payload, user)

            # Handle JSONResponse (some backends return JSONResponse instead of dict)
            if hasattr(response, "body"):
                # It's a Response object, extract the body
                import json as json_module

                try:
                    response = json_module.loads(response.body.decode("utf-8"))
                except Exception:
                    raise ValueError(f"Failed to parse JSONResponse body: {response}")

            provider_error = self._extract_provider_error(response)
            if provider_error:
                try:
                    response_repr = json.dumps(response, ensure_ascii=False, indent=2)
                except Exception:
                    response_repr = repr(response)
                raise ValueError(
                    f"Upstream provider error: {provider_error}\n"
                    f"Full response:\n{response_repr}"
                )

            if (
                not response
                or not isinstance(response, dict)
                or "choices" not in response
                or not response["choices"]
            ):
                try:
                    response_repr = json.dumps(response, ensure_ascii=False, indent=2)
                except Exception:
                    response_repr = repr(response)
                raise ValueError(
                    f"LLM response format incorrect or empty: {type(response).__name__}\n"
                    f"Full response:\n{response_repr}"
                )

            summary = response["choices"][0]["message"]["content"].strip()

            await self._log(
                f"[🤖 LLM Call] ✅ Successfully received summary",
                log_type="success",
                event_call=__event_call__,
            )

            return summary

        except Exception as e:
            error_msg = str(e)
            # Handle specific error messages
            if "Model not found" in error_msg:
                error_message = f"Summary model '{model}' not found."
            else:
                error_message = f"Summary LLM Error ({model}): {error_msg}"
            if not self.valves.summary_model:
                error_message += (
                    "\n[Hint] You did not specify a summary_model, so the filter attempted to use the current conversation's model. "
                    "If this is a pipeline (Pipe) model or an incompatible model, please specify a compatible summary model (e.g., 'gemini-2.5-flash') in the configuration."
                )

            if __event_call__:
                await self._emit_frontend_console_log(
                    f"[🤖 LLM Call] ❌ {error_message}",
                    log_type="error",
                    event_call=__event_call__,
                    force=True,
                )
            await self._log(
                f"[🤖 LLM Call] ❌ {error_message}",
                log_type="error",
                event_call=None,
            )

            wrapped_error = Exception(error_message)
            setattr(wrapped_error, "_frontend_logged", True)
            raise wrapped_error
