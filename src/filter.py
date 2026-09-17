# ── filter.py · Filter entry point (inlet/outlet/Valves) ──────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.

class Filter(I18nMixin, TokenMixin, DBMixin, ToolCallMixin, CompressionMixin,
            SummarizeMixin, ExternalRefsMixin, ConsoleMixin):
    """Open WebUI filter: async context compression (entry point)."""
    def __init__(self):
        self.valves = self.Valves()
        self._owui_db = owui_db
        self._db_engine = owui_engine
        self._fallback_session_factory = (
            sessionmaker(bind=self._db_engine) if self._db_engine else None
        )
        self._model_thresholds_cache: Optional[Dict[str, Any]] = None

        # Fallback mapping for variants not in TRANSLATIONS keys
        self.fallback_map = {
            "en-CA": "en-US",
            "en-GB": "en-US",
            "en-AU": "en-US",
        }

        # Concurrency control: Lock per chat session
        self._chat_locks = {}
        self._pending_inlet_messages: Dict[str, List[Dict[str, Any]]] = {}
        self._init_database()
    class Valves(BaseModel):
        priority: int = Field(
            default=10, description="Priority level for the filter operations."
        )
        # Token related parameters
        compression_threshold_tokens: int = Field(
            default=64000,
            ge=0,
            description="When total context Token count exceeds this value, trigger compression (Global Default)",
        )
        max_context_tokens: int = Field(
            default=128000,
            ge=0,
            description="Hard limit for context. Exceeding this value will force removal of earliest messages (Global Default)",
        )
        model_thresholds: str = Field(
            default="",
            description="Per-model threshold overrides. Format: model_id:compression_threshold:max_context (comma-separated). Example: gpt-4:8000:32000, claude-3:100000:200000",
        )

        keep_first: int = Field(
            default=0,
            ge=0,
            description="Keep the first N non-system messages plus all interleaved system messages. Set to 0 to disable.",
        )
        keep_last: int = Field(
            default=6, ge=0, description="Always keep the last N full messages."
        )
        summary_model: Optional[str] = Field(
            default=None,
            description="The model ID used to generate the summary. If empty, uses the current conversation's model. Used to match configurations in model_thresholds.",
        )
        summary_model_max_context: int = Field(
            default=0,
            ge=0,
            description="Max context tokens for the summary model. If 0, falls back to model_thresholds or global max_context_tokens. Example: gemini-flash=1000000, gpt-4o-mini=128000.",
        )
        max_summary_tokens: int = Field(
            default=16384,
            ge=1,
            description="The maximum number of tokens for the summary.",
        )
        summary_temperature: float = Field(
            default=0.1,
            ge=0.0,
            le=2.0,
            description="The temperature for summary generation.",
        )
        summary_chat_template_kwargs: str = Field(
            default='{"enable_thinking": false}',
            description="JSON object of chat-template variables sent to the summary model (OpenAI-compatible backends that honor chat_template_kwargs, e.g. vLLM/llama.cpp). Example: {\"enable_thinking\": false} to turn off thinking for Qwen3/Gemma. Leave empty to send none.",
        )
        debug_mode: bool = Field(
            default=False, description="Enable detailed logging for debugging."
        )
        show_debug_log: bool = Field(
            default=False, description="Show debug logs in the frontend console"
        )
        show_token_usage_status: bool = Field(
            default=True, description="Show token usage status notification"
        )
        token_usage_status_threshold: int = Field(
            default=80,
            ge=0,
            le=100,
            description="Only show token usage status when usage exceeds this percentage (0-100). Set to 0 to always show.",
        )
        enable_tool_output_trimming: bool = Field(
            default=True,
            description="Enable trimming of large tool outputs (only works with native function calling).",
        )
        tool_trim_threshold_chars: int = Field(
            default=600,
            ge=1,
            description="Trim native tool outputs when their total content length reaches this many characters.",
        )
    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: dict = None,
        __request__: Request = None,
        __model__: dict = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __event_call__: Callable[[Any], Awaitable[None]] = None,
    ) -> dict:
        """
        Executed before sending to the LLM.
        Compression Strategy: Only responsible for injecting existing summaries, no Token calculation.
        """

        if self._should_skip_compression(body, __model__):
            if self.valves.debug_mode:
                logger.info(
                    "[Inlet] Skipping compression: copilot_sdk detected in base model"
                )
            return body

        messages = body.get("messages", [])
        user_ctx = await self._get_user_context(__user__, __event_call__)
        lang = user_ctx["user_language"]

        normalized_tool_call_count = self._normalize_native_tool_call_ids(messages)
        if (
            normalized_tool_call_count > 0
            and self.valves.show_debug_log
            and __event_call__
        ):
            await self._log(
                f"[Inlet] 🪪 Normalized {normalized_tool_call_count} overlong tool call ID(s).",
                event_call=__event_call__,
            )

        # --- Native Tool Output Trimming (Opt-in, only for native function calling) ---
        function_calling_mode = self._get_function_calling_mode(body)
        is_native_func_calling = function_calling_mode == "native"

        if self.valves.show_debug_log and __event_call__:
            trimming_state = (
                "enabled" if self.valves.enable_tool_output_trimming else "disabled"
            )
            await self._log(
                "[Inlet] ✂️ Tool trimming check: "
                f"state={trimming_state}, function_calling={function_calling_mode or 'unset'}, "
                f"message_count={len(messages)}",
                event_call=__event_call__,
            )

        if self.valves.enable_tool_output_trimming and is_native_func_calling:
            trimmed_count, trim_debug = self._trim_native_tool_outputs(
                messages,
                lang,
                collect_debug=bool(self.valves.show_debug_log and __event_call__),
            )
        elif self.valves.show_debug_log and __event_call__:
            skip_reason = (
                "tool trimming disabled"
                if not self.valves.enable_tool_output_trimming
                else f"function_calling={function_calling_mode or 'unset'}"
            )
            await self._log(
                f"[Inlet] ✂️ Tool trimming skipped: {skip_reason}.",
                event_call=__event_call__,
            )

        chat_ctx = self._get_chat_context(body, __metadata__)
        chat_id = chat_ctx["chat_id"]

        body = await self._handle_external_chat_references(
            body,
            user_data=__user__,
            __event_call__=__event_call__,
            __request__=__request__,
        )
        messages = body.get("messages", [])

        # Extract system prompt for accurate token calculation
        # 1. For custom models: check DB (Models.get_model_by_id)
        # 2. For base models: check messages for role='system'
        system_prompt_content = None

        # Try to get from DB (custom model)
        # Try to get from DB (custom model)
        try:
            model_id = body.get("model")
            if model_id:
                if self.valves.show_debug_log and __event_call__:
                    await self._log(
                        f"[Inlet] 🔍 Attempting DB lookup for model: {model_id}",
                        event_call=__event_call__,
                    )

                # Clean model ID if needed (though get_model_by_id usually expects the full ID)
                # Version-aware DB call (async on >=0.9.0, sync on <0.9.0)
                model_obj = await _call_db(Models.get_model_by_id, model_id)

                if model_obj:
                    if self.valves.show_debug_log and __event_call__:
                        await self._log(
                            f"[Inlet] ✅ Model found in DB: {model_obj.name} (ID: {model_obj.id})",
                            event_call=__event_call__,
                        )

                    if model_obj.params:
                        try:
                            params = model_obj.params
                            # Handle case where params is a JSON string
                            if isinstance(params, str):
                                params = json.loads(params)
                            # Convert Pydantic model to dict if needed
                            elif hasattr(params, "model_dump"):
                                params = params.model_dump()
                            elif hasattr(params, "dict"):
                                params = params.dict()

                            # Now params should be a dict
                            if isinstance(params, dict):
                                system_prompt_content = params.get("system")
                            else:
                                # Fallback: try getattr
                                system_prompt_content = getattr(params, "system", None)

                            if system_prompt_content:
                                if self.valves.show_debug_log and __event_call__:
                                    await self._log(
                                        f"[Inlet] 📝 System prompt found in DB params ({len(system_prompt_content)} chars)",
                                        event_call=__event_call__,
                                    )
                            else:
                                if self.valves.show_debug_log and __event_call__:
                                    await self._log(
                                        f"[Inlet] ⚠️ 'system' key missing in model params",
                                        event_call=__event_call__,
                                    )
                        except Exception as e:
                            if self.valves.show_debug_log and __event_call__:
                                await self._log(
                                    f"[Inlet] ❌ Failed to parse model params: {e}",
                                    log_type="error",
                                    event_call=__event_call__,
                                )

                    else:
                        if self.valves.show_debug_log and __event_call__:
                            await self._log(
                                f"[Inlet] ⚠️ Model params are empty",
                                event_call=__event_call__,
                            )
                else:
                    if self.valves.show_debug_log and __event_call__:
                        await self._log(
                            f"[Inlet] ℹ️ Not a custom model, skipping custom system prompt check",
                            event_call=__event_call__,
                        )

        except Exception as e:
            if self.valves.show_debug_log and __event_call__:
                await self._log(
                    f"[Inlet] ❌ Error fetching system prompt from DB: {e}",
                    log_type="error",
                    event_call=__event_call__,
                )
            if self.valves.debug_mode:
                logger.error(f"[Inlet] Error fetching system prompt from DB: {e}")

        # Fall back to checking messages (base model or already included)
        if not system_prompt_content:
            for msg in messages:
                if msg.get("role") == "system":
                    system_prompt_content = msg.get("content", "")
                    break

        # Build system_prompt_msg for token calculation
        system_prompt_msg = None
        if system_prompt_content:
            system_prompt_msg = {"role": "system", "content": system_prompt_content}
            if self.valves.debug_mode:
                logger.info(
                    f"[Inlet] Found system prompt ({len(system_prompt_content)} chars). Including in budget."
                )

        # Log message statistics (Moved here to include extracted system prompt)
        if self.valves.show_debug_log and __event_call__:
            try:
                msg_stats = {
                    "user": 0,
                    "assistant": 0,
                    "system": 0,
                    "total": len(messages),
                }
                for msg in messages:
                    role = msg.get("role", "unknown")
                    if role in msg_stats:
                        msg_stats[role] += 1

                # If system prompt was extracted from DB/Model but not in messages, count it
                if system_prompt_content:
                    # Check if it's already counted (i.e., was in messages)
                    is_in_messages = any(m.get("role") == "system" for m in messages)
                    if not is_in_messages:
                        msg_stats["system"] += 1
                        msg_stats["total"] += 1

                stats_str = f"Total: {msg_stats['total']} | User: {msg_stats['user']} | Assistant: {msg_stats['assistant']} | System: {msg_stats['system']}"
                await self._log(
                    f"[Inlet] Message Stats: {stats_str}", event_call=__event_call__
                )
            except Exception as e:
                logger.error(f"[Inlet] Error logging message stats: {e}")

        if not chat_id:
            await self._log(
                "[Inlet] ❌ Missing chat_id in metadata, skipping compression",
                log_type="error",
                event_call=__event_call__,
            )
            return body

        if self.valves.debug_mode or self.valves.show_debug_log:
            await self._log(
                f"\n{'='*60}\n[Inlet] Chat ID: {chat_id}\n[Inlet] Received {len(messages)} messages",
                event_call=__event_call__,
            )

            # Log custom model configurations
            raw_config = self.valves.model_thresholds
            parsed_configs = self._parse_model_thresholds()

            if raw_config:
                config_list = [
                    f"{model}: {cfg['compression_threshold_tokens']}t/{cfg['max_context_tokens']}t"
                    for model, cfg in parsed_configs.items()
                ]

                if config_list:
                    await self._log(
                        f"[Inlet] 📋 Model Configs (Raw: '{raw_config}'): {', '.join(config_list)}",
                        event_call=__event_call__,
                    )
                else:
                    await self._log(
                        f"[Inlet] ⚠️ Invalid Model Configs (Raw: '{raw_config}'): No valid configs parsed. Expected format: 'model_id:threshold:max_context'",
                        log_type="warning",
                        event_call=__event_call__,
                    )
            else:
                await self._log(
                    f"[Inlet] 📋 Model Configs: No custom configuration (Global defaults only)",
                    event_call=__event_call__,
                )

        # Log the aligned compression boundary using the same original-history
        # coordinate mapping as outlet/async summary generation.
        target_compressed_count = self._calculate_target_compressed_count(messages)

        await self._log(
            f"[Inlet] Recorded target compression progress: {target_compressed_count}",
            event_call=__event_call__,
        )

        # Load summary record
        summary_record = await self._load_summary_record(chat_id)

        # Calculate effective_keep_first to ensure all system messages are protected
        effective_keep_first = self._get_effective_keep_first(messages)

        final_messages = []
        external_refs_injected_count = 0

        if summary_record:
            # Summary exists, build view: [Head] + [Summary Message] + [Tail]
            # Tail is all messages after the last compression point
            compressed_count = summary_record.compressed_message_count

            # Ensure compressed_count is reasonable
            if compressed_count > len(messages):
                compressed_count = max(0, len(messages) - self.valves.keep_last)

            # 1. Head messages (Keep First)
            head_messages = []
            if effective_keep_first > 0:
                head_messages = messages[:effective_keep_first]

            # 2. Tail messages (Tail) - All messages starting from the last compression point.
            # Align legacy/raw progress to an atomic boundary so old summary rows do not
            # reintroduce orphaned tool messages into the retained tail.
            raw_start_index = max(compressed_count, effective_keep_first)
            start_index = self._align_tail_start_to_atomic_boundary(
                messages, raw_start_index, effective_keep_first
            )

            # --- Extract Preserved System Messages from the Gap ---
            # Any system message in the gap (messages[effective_keep_first:start_index])
            # must be preserved according to policy.
            gap_messages = messages[effective_keep_first:start_index]
            preserved_system_messages = [
                msg
                for msg in gap_messages
                if isinstance(msg, dict) and msg.get("role") == "system"
            ]

            # 3. Summary message (Inserted as Assistant message)
            external_refs = body.pop("__external_references__", None)
            summary_msg = self._build_summary_message(
                summary_record.summary,
                lang,
                start_index,
            )

            if external_refs:
                external_content = external_refs.get("content", "")
                if external_content:
                    external_refs_injected_count = len(
                        external_refs.get("references", [])
                    )
                    summary_msg["content"] = (
                        f"<external_references>\n{external_content}\n</external_references>\n\n"
                        + summary_msg["content"]
                    )
                    summary_msg["metadata"]["external_references"] = external_refs.get(
                        "references", []
                    )

            tail_messages = messages[start_index:]

            # --- Preflight Check & Budgeting (Simplified) ---

            # Assemble candidate messages (for output)
            candidate_messages = (
                head_messages
                + [summary_msg]
                + preserved_system_messages
                + tail_messages
            )

            # Prepare messages for token calculation (include system prompt if missing)
            calc_messages = candidate_messages
            if system_prompt_msg:
                # Check if system prompt is already in head_messages
                is_in_head = any(m.get("role") == "system" for m in head_messages)
                if not is_in_head:
                    calc_messages = [system_prompt_msg] + candidate_messages

            # Get max context limit
            model = self._clean_model_id(body.get("model"))
            thresholds = self._get_model_thresholds(model)
            max_context_tokens = thresholds.get(
                "max_context_tokens", self.valves.max_context_tokens
            )

            # --- Fast Estimation Check ---
            estimated_tokens = self._estimate_messages_tokens(calc_messages)

            # Since this is a hard limit check, only skip precise calculation if we are far below it (margin of 15%)
            # max_context_tokens == 0 means "no limit", skip reduction entirely
            if max_context_tokens <= 0:
                total_tokens = estimated_tokens
                await self._log(
                    f"[Inlet] 🔎 No max_context_tokens limit set (0). Skipping reduction. Est: {total_tokens}t",
                    event_call=__event_call__,
                )
            elif estimated_tokens < max_context_tokens * 0.85:
                total_tokens = estimated_tokens
                await self._log(
                    "[Inlet] 🔎 Sent-context preflight (estimated)\n"
                    f"sent_context_tokens={total_tokens} | max_context_tokens={max_context_tokens} | status=well_within_limit",
                    event_call=__event_call__,
                )
            else:
                # Calculate exact total tokens via tiktoken
                total_tokens = await asyncio.to_thread(
                    self._calculate_messages_tokens, calc_messages
                )

                # Preflight Check Log
                await self._log(
                    "[Inlet] 🔎 Sent-context preflight (precise)\n"
                    f"sent_context_tokens={total_tokens} | max_context_tokens={max_context_tokens} | usage={(total_tokens/max_context_tokens*100):.1f}%",
                    event_call=__event_call__,
                )

                # Identify atomic groups to avoid breaking tool-calling context
                atomic_groups = self._get_atomic_groups(tail_messages)

                while total_tokens > max_context_tokens and len(atomic_groups) > 1:
                    # Strategy 1: Structure-Aware Assistant Trimming (Optional, only for non-tool messages)
                    # For simplicity and reliability in this fix, we prioritize Group-Drop over partial trim
                    # if a group contains tool calls.

                    # Strategy 2: Drop Oldest Atomic Group Entirely
                    dropped_group_indices = atomic_groups.pop(0)
                    # Note: indices in dropped_group_indices are relative to ORIGINAL tail_messages
                    # But since we are popping from tail_messages itself, we need to be careful.

                    # Extract and drop messages in this group from the actual list
                    # Since we always pop group 0, we pop len(dropped_group_indices) times from front
                    dropped_tokens = 0
                    for _ in range(len(dropped_group_indices)):
                        dropped = tail_messages.pop(0)
                        if total_tokens == estimated_tokens:
                            dropped_tokens += self._estimate_content_tokens(
                                dropped.get("content", "")
                            )
                        else:
                            dropped_tokens += self._count_tokens(
                                str(dropped.get("content", ""))
                            )

                    total_tokens -= dropped_tokens

                    if self.valves.show_debug_log and __event_call__:
                        await self._log(
                            f"[Inlet] 🗑️ Dropped atomic group ({len(dropped_group_indices)} msgs) to fit context. Tokens: {dropped_tokens}",
                            event_call=__event_call__,
                        )

                # Re-assemble
                candidate_messages = (
                    head_messages
                    + [summary_msg]
                    + preserved_system_messages
                    + tail_messages
                )

                await self._log(
                    "[Inlet] ✂️ Sent-context history reduced\n"
                    f"sent_context_tokens={total_tokens} | tail_size={len(tail_messages)}",
                    event_call=__event_call__,
                )

            final_messages = candidate_messages

            # Calculate detailed token stats for logging
            summary_content = summary_msg.get("content", "")
            if total_tokens == estimated_tokens:
                system_tokens = (
                    self._estimate_content_tokens(system_prompt_msg.get("content", ""))
                    if system_prompt_msg
                    else 0
                )
                head_tokens = self._estimate_messages_tokens(head_messages)
                summary_tokens = self._estimate_content_tokens(summary_content)
                preserved_system_tokens = self._estimate_messages_tokens(
                    preserved_system_messages
                )
                tail_tokens = self._estimate_messages_tokens(tail_messages)
            else:
                system_tokens = (
                    self._count_tokens(system_prompt_msg.get("content", ""))
                    if system_prompt_msg
                    else 0
                )
                head_tokens = self._calculate_messages_tokens(head_messages)
                summary_tokens = self._count_tokens(summary_content)
                preserved_system_tokens = self._calculate_messages_tokens(
                    preserved_system_messages
                )
                tail_tokens = self._calculate_messages_tokens(tail_messages)

            system_info = (
                f"System({system_tokens + preserved_system_tokens}t)"
                if (system_prompt_msg or preserved_system_messages)
                else "System(0t)"
            )

            total_section_tokens = (
                system_tokens
                + head_tokens
                + summary_tokens
                + preserved_system_tokens
                + tail_tokens
            )

            await self._log(
                "[Inlet] ✅ Sent context assembled\n"
                f"sent_context_tokens={total_section_tokens} | {system_info} + Head({len(head_messages)} msg, {head_tokens}t) + Summary({summary_tokens}t) + Tail({len(tail_messages)} msg, {tail_tokens}t)",
                log_type="success",
                event_call=__event_call__,
            )

            # Prepare status message (Context Usage format)
            if max_context_tokens > 0:
                usage_ratio = total_section_tokens / max_context_tokens
                # Only show status if threshold is met
                if self._should_show_status(usage_ratio):
                    status_msg = self._get_translation(
                        lang,
                        "status_context_usage",
                        tokens=total_section_tokens,
                        max_tokens=max_context_tokens,
                        ratio=f"{usage_ratio*100:.1f}",
                    )
                    if usage_ratio > 0.9:
                        status_msg += self._get_translation(lang, "status_high_usage")

                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": status_msg,
                                    "done": True,
                                },
                            }
                        )
            else:
                # For the case where max_context_tokens is 0, show summary info without threshold check
                if self.valves.show_token_usage_status and __event_emitter__:
                    status_msg = self._get_translation(
                        lang, "status_loaded_summary", count=compressed_count
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

        else:
            external_refs = body.pop("__external_references__", None)

            if external_refs and external_refs.get("content") and messages:
                external_content = external_refs.get("content", "")
                external_refs_injected_count = len(external_refs.get("references", []))
                ref_msg = {
                    "role": "assistant",
                    "content": (
                        f"<external_references>\n{external_content}\n</external_references>\n\n"
                        + "Here are references to other conversations that may be relevant to this discussion."
                    ),
                    "metadata": {
                        "is_summary": True,
                        "is_external_references": True,
                        "source": "external_references",
                        "covered_until": effective_keep_first,
                        "external_references": external_refs.get("references", []),
                    },
                }

                head_messages = messages[:effective_keep_first]
                tail_messages = messages[effective_keep_first:]
                candidate_messages = head_messages + [ref_msg] + tail_messages

                if __event_call__:
                    await self._log(
                        f"[Inlet] 📎 💉 Injected {external_refs_injected_count} external chat reference(s) as contextual block (head: {len(head_messages)}, tail: {len(tail_messages)})",
                        event_call=__event_call__,
                    )
            else:
                candidate_messages = messages if messages else []

            if not candidate_messages:
                return body

            final_messages = candidate_messages

            calc_messages = candidate_messages
            if system_prompt_msg:
                is_in_messages = any(
                    m.get("role") == "system" for m in candidate_messages
                )
                if not is_in_messages:
                    calc_messages = [system_prompt_msg] + candidate_messages

            # Get max context limit
            model = self._clean_model_id(body.get("model"))
            thresholds = self._get_model_thresholds(model) or {}
            max_context_tokens = thresholds.get(
                "max_context_tokens", self.valves.max_context_tokens
            )

            # --- Fast Estimation Check ---
            estimated_tokens = self._estimate_messages_tokens(calc_messages)

            # Only skip precise calculation if we are clearly below the limit
            # max_context_tokens == 0 means "no limit", skip reduction entirely
            if max_context_tokens <= 0:
                total_tokens = estimated_tokens
                await self._log(
                    f"[Inlet] 🔎 No max_context_tokens limit set (0). Skipping reduction. Est: {total_tokens}t",
                    event_call=__event_call__,
                )
            elif estimated_tokens < max_context_tokens * 0.85:
                total_tokens = estimated_tokens
                await self._log(
                    f"[Inlet] 🔎 Fast limit check (Est): {total_tokens}t / {max_context_tokens}t",
                    event_call=__event_call__,
                )
            else:
                total_tokens = await asyncio.to_thread(
                    self._calculate_messages_tokens, calc_messages
                )

            if total_tokens > max_context_tokens and max_context_tokens > 0:
                await self._log(
                    f"[Inlet] ⚠️ Original messages ({total_tokens} Tokens) exceed limit ({max_context_tokens}). Reducing history...",
                    log_type="warning",
                    event_call=__event_call__,
                )

                # Use atomic grouping to preserve tool-calling integrity
                trimmable = candidate_messages[effective_keep_first:]
                atomic_groups = self._get_atomic_groups(trimmable)

                # To follow policy "system messages never lost", we maintain a list of
                # system messages that were part of dropped groups.
                dropped_but_preserved_systems = []

                while total_tokens > max_context_tokens and len(atomic_groups) > 1:
                    dropped_group_indices = atomic_groups.pop(0)
                    dropped_tokens = 0
                    for _ in range(len(dropped_group_indices)):
                        dropped = trimmable.pop(0)

                        # Absolute protections:
                        # 1. External references (often large and specialized)
                        # 2. System messages (instructions)
                        if self._is_external_reference_message(dropped):
                            trimmable.insert(0, dropped)
                            # Stop dropping this group if we hit a protected message
                            # (Though groups should be pure, this is a safety net)
                            break

                        if (
                            isinstance(dropped, dict)
                            and dropped.get("role") == "system"
                        ):
                            dropped_but_preserved_systems.append(dropped)
                            # Even if preserved, it counts as "dropped" from the trimmable flow
                            # to avoid infinite loop, but its tokens remain in the budget.
                            # We don't subtract its tokens here.
                            continue

                        if total_tokens == estimated_tokens:
                            dropped_tokens += self._estimate_content_tokens(
                                dropped.get("content", "")
                            )
                        else:
                            dropped_tokens += self._count_tokens(
                                str(dropped.get("content", ""))
                            )
                    total_tokens -= dropped_tokens

                # Re-assemble: [Head] + [Preserved Systems from Dropped Groups] + [Remaining Trimmable/Tail]
                candidate_messages = (
                    candidate_messages[:effective_keep_first]
                    + dropped_but_preserved_systems
                    + trimmable
                )

                await self._log(
                    f"[Inlet] ✂️ Messages reduced (atomic). New total: {total_tokens} Tokens",
                    event_call=__event_call__,
                )

            # Send status notification (Context Usage format)
            if max_context_tokens > 0:
                usage_ratio = total_tokens / max_context_tokens
                # Only show status if threshold is met
                if self._should_show_status(usage_ratio):
                    status_msg = self._get_translation(
                        lang,
                        "status_context_usage",
                        tokens=total_tokens,
                        max_tokens=max_context_tokens,
                        ratio=f"{usage_ratio*100:.1f}",
                    )
                    if usage_ratio > 0.9:
                        status_msg += self._get_translation(lang, "status_high_usage")

                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": status_msg,
                                    "done": True,
                                },
                            }
                        )

        body["messages"] = final_messages
        self._capture_pending_inlet_messages(chat_id, final_messages)

        await self._log(
            f"[Inlet] ✅ Final send\nsent_message_count={len(body['messages'])}\n{'='*60}\n",
            event_call=__event_call__,
        )

        metadata = body.get("metadata", {})
        files = metadata.get("files", [])
        if files:
            new_files = [f for f in files if f.get("type") != "chat"]
            if len(new_files) != len(files):
                metadata["files"] = new_files
                body["metadata"] = metadata
                if __event_call__:
                    await self._log(
                        f"[Inlet] 🗑️ Removed {len(files) - len(new_files)} chat reference(s) from files to prevent RAG",
                        event_call=__event_call__,
                    )

        if external_refs_injected_count > 0 and __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": self._get_translation(
                            lang,
                            "status_external_refs_injected",
                            count=external_refs_injected_count,
                        ),
                        "done": True,
                    },
                }
            )

        return body
    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: dict = None,
        __model__: dict = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __event_call__: Callable[[Any], Awaitable[None]] = None,
        __request__: Request = None,
    ) -> dict:
        """
        Executed after the LLM response is complete.
        Calculates Token count in the background and triggers summary generation (does not block current response, does not affect content output).
        """
        # Check if compression should be skipped (e.g., for copilot_sdk)
        if self._should_skip_compression(body, __model__):
            if self.valves.debug_mode:
                logger.info(
                    "[Outlet] Skipping compression: copilot_sdk detected in base model"
                )
            if self.valves.show_debug_log and __event_call__:
                await self._log(
                    "[Outlet] ⏭️ Skipping compression: copilot_sdk detected",
                    event_call=__event_call__,
                )
            return body

        # Get user context for i18n
        user_ctx = await self._get_user_context(__user__, __event_call__)
        lang = user_ctx["user_language"]

        chat_ctx = self._get_chat_context(body, __metadata__)
        chat_id = chat_ctx["chat_id"]
        if not chat_id:
            await self._log(
                "[Outlet] ❌ Missing chat_id in metadata, skipping compression",
                log_type="error",
                event_call=__event_call__,
            )
            return body
        model = body.get("model") or ""
        messages = body.get("messages", [])

        # Unfold compact tool messages to align with inlet's exact coordinate system.
        # Native tool-calling payloads in outlet can miss hidden `output` fields, so
        # preserve the older DB fallback there only.
        function_calling_mode = self._get_function_calling_mode(body)
        if function_calling_mode == "native":
            db_messages = await self._load_full_chat_messages(chat_id)
            messages_to_unfold = (
                db_messages
                if (db_messages and len(db_messages) >= len(messages))
                else messages
            )
            summary_messages = self._unfold_messages(messages_to_unfold)
            if messages_to_unfold is db_messages:
                message_source = (
                    "outlet-db-unfolded"
                    if len(summary_messages) != len(db_messages)
                    else "outlet-db"
                )
            else:
                message_source = (
                    "outlet-body-unfolded"
                    if len(summary_messages) != len(messages)
                    else "outlet-body"
                )
        else:
            summary_messages = self._unfold_messages(messages)
            message_source = (
                "outlet-body-unfolded"
                if len(summary_messages) != len(messages)
                else "outlet-body"
            )

        restored_count_before = len(summary_messages)
        summary_messages = self._restore_pending_inlet_messages(
            chat_id, summary_messages
        )
        if len(summary_messages) != restored_count_before:
            message_source = f"{message_source}+pending"

        # Calculate target compression progress directly, then align it to an atomic
        # boundary so the saved summary never cuts through a tool-calling block.
        target_compressed_count = self._calculate_target_compressed_count(
            summary_messages
        )

        summary_body = dict(body)
        summary_body["messages"] = summary_messages

        # Process Token calculation and summary generation asynchronously in the background
        # Use a lock to prevent multiple concurrent summary tasks for the same chat
        chat_lock = self._get_chat_lock(chat_id)

        if chat_lock.locked():
            if self.valves.debug_mode:
                logger.info(
                    f"[Outlet] Skipping summary task for {chat_id}: Task already in progress"
                )
            return body

        asyncio.create_task(
            self._locked_summary_task(
                chat_lock,
                chat_id,
                model,
                summary_body,
                __user__,
                target_compressed_count,
                lang,
                __event_emitter__,
                __event_call__,
                __request__,
            )
        )

        return body
