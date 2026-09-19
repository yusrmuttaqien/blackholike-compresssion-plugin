# ── compression.py · History reconstruction, thresholds, compression orchestration 
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.

# Rate limiting for the background context-length self-heal task:
# at most one live re-query per model per interval.
_context_heal_last_checked: Dict[str, float] = {}
_CONTEXT_HEAL_INTERVAL_SECONDS = 300


class CompressionMixin:

    """History reconstruction, thresholds, compression orchestration."""

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create an asyncio lock for a specific chat ID."""
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            # Bound growth: drop idle locks once the map gets large.
            if len(self._chat_locks) >= 128:
                self._chat_locks = {
                    cid: lk for cid, lk in self._chat_locks.items() if lk.locked()
                }
            lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        return lock

    def _capture_pending_inlet_messages(
        self, chat_id: str, messages: List[Dict[str, Any]]
    ) -> None:
        """Persist transient inlet-only messages so outlet can rebuild sent context."""
        pending_messages = []
        for message in messages:
            if not isinstance(message, dict):
                continue

            metadata = message.get("metadata", {})
            if not isinstance(metadata, dict):
                continue

            if metadata.get("is_external_references") or metadata.get(
                "external_references"
            ):
                pending_messages.append(deepcopy(message))

        if pending_messages:
            self._pending_inlet_messages[chat_id] = pending_messages
            # Bound growth: entries hold deep-copied messages; drop the oldest
            # chat's pending messages once the map gets large.
            if len(self._pending_inlet_messages) > 64:
                self._pending_inlet_messages.pop(
                    next(iter(self._pending_inlet_messages)), None
                )
        else:
            self._pending_inlet_messages.pop(chat_id, None)

    def _restore_pending_inlet_messages(
        self, chat_id: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Reapply transient inlet-only messages after outlet rebuilds persisted history."""
        pending_messages = self._pending_inlet_messages.pop(chat_id, None)
        if not pending_messages:
            return messages

        restored_messages = list(messages)
        base_keep_first = self._get_effective_keep_first(messages)

        for pending_message in pending_messages:
            if not isinstance(pending_message, dict):
                continue

            pending_content = pending_message.get("content", "")
            if any(
                isinstance(existing, dict)
                and existing.get("role") == pending_message.get("role")
                and existing.get("content", "") == pending_content
                for existing in restored_messages
            ):
                continue

            metadata = pending_message.get("metadata", {})
            covered_until = (
                metadata.get("covered_until", base_keep_first)
                if isinstance(metadata, dict)
                else base_keep_first
            )
            try:
                insert_index = int(covered_until)
            except Exception:
                insert_index = base_keep_first

            insert_index = max(0, min(insert_index, len(restored_messages)))
            restored_messages.insert(insert_index, deepcopy(pending_message))

        return restored_messages

    def _is_summary_message(self, message: Dict[str, Any]) -> bool:
        """Return True when the message is this filter's injected summary marker."""
        metadata = message.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        return bool(
            metadata.get("is_summary")
            and metadata.get("source") == SUMMARY_METADATA_SOURCE
        )

    def _build_summary_message(
        self, summary_text: str, lang: str, covered_until: int
    ) -> Dict[str, Any]:
        """Create a summary marker message with original-history progress metadata."""
        summary_content = (
            self._get_translation(lang, "summary_prompt_prefix")
            + f"{summary_text}"
            + self._get_translation(lang, "summary_prompt_suffix")
        )
        return {
            "role": "assistant",
            "content": summary_content,
            "metadata": {
                "is_summary": True,
                "source": SUMMARY_METADATA_SOURCE,
                "covered_until": max(0, int(covered_until)),
            },
        }

    def _is_external_reference_message(self, message: Dict[str, Any]) -> bool:
        metadata = message.get("metadata", {})
        if not isinstance(metadata, dict):
            return False
        return bool(
            metadata.get("is_external_references")
            or metadata.get("source") == "external_references"
        )

    def _get_summary_view_state(self, messages: List[Dict]) -> Dict[str, Optional[int]]:
        """Inspect the current message view and recover summary marker metadata."""
        for index, message in enumerate(messages):
            if self._is_external_reference_message(message):
                continue
            if not self._is_summary_message(message):
                continue

            metadata = message.get("metadata", {})
            covered_until = metadata.get("covered_until", 0)
            if not isinstance(covered_until, int) or covered_until < 0:
                covered_until = 0

            return {
                "summary_index": index,
                "base_progress": covered_until,
            }

        return {"summary_index": None, "base_progress": 0}

    def _get_original_history_count(self, messages: List[Dict]) -> int:
        """Map the current visible message list back to original-history size."""
        summary_state = self._get_summary_view_state(messages)
        summary_index = summary_state["summary_index"]
        base_progress = summary_state["base_progress"] or 0

        if summary_index is None:
            return len(messages)

        return base_progress + max(0, len(messages) - summary_index - 1)

    def _calculate_target_compressed_count(self, messages: List[Dict]) -> int:
        """Calculate the next summary boundary in original-history coordinates."""
        summary_state = self._get_summary_view_state(messages)
        summary_index = summary_state["summary_index"]
        base_progress = summary_state["base_progress"] or 0

        original_count = self._get_original_history_count(messages)
        raw_target = max(base_progress, original_count - self.valves.keep_last)

        if summary_index is None:
            protected_prefix = self._get_effective_keep_first(messages)
            return self._align_tail_start_to_atomic_boundary(
                messages, raw_target, protected_prefix
            )

        if raw_target <= base_progress:
            return base_progress

        tail_messages = messages[summary_index + 1 :]
        local_target = raw_target - base_progress
        aligned_local_target = self._align_tail_start_to_atomic_boundary(
            tail_messages, local_target, 0
        )
        return base_progress + aligned_local_target

    def _reconstruct_active_history_branch(
        self, history_messages: Any, current_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Rebuild the active chat branch from OpenWebUI `history.messages` data."""
        if not isinstance(history_messages, dict) or not history_messages:
            return []

        if isinstance(current_id, str) and current_id in history_messages:
            ordered_messages: List[Dict[str, Any]] = []
            visited = set()
            cursor = current_id

            while isinstance(cursor, str) and cursor and cursor not in visited:
                visited.add(cursor)
                node = history_messages.get(cursor)
                if not isinstance(node, dict):
                    break

                ordered_messages.append(deepcopy(node))
                cursor = node.get("parentId") or node.get("parent_id")

            if ordered_messages:
                ordered_messages.reverse()
                return ordered_messages

        sortable_messages = []
        for index, node in enumerate(history_messages.values()):
            if not isinstance(node, dict):
                continue

            timestamp = node.get("timestamp")
            if not isinstance(timestamp, (int, float)):
                timestamp = node.get("created_at")
            if not isinstance(timestamp, (int, float)):
                timestamp = index

            sortable_messages.append((float(timestamp), index, deepcopy(node)))

        sortable_messages.sort(key=lambda item: (item[0], item[1]))
        return [message for _, _, message in sortable_messages]

    async def _load_full_chat_messages(self, chat_id: str) -> List[Dict[str, Any]]:
        """Load the full persisted chat history for summary decisions when available."""
        if not chat_id or Chats is None:
            return []

        try:
            chat_record = await _call_db(Chats.get_chat_by_id, chat_id)
        except Exception as exc:
            logger.warning(f"[Chat Load] Failed to fetch chat {chat_id}: {exc}")
            return []

        chat_payload = getattr(chat_record, "chat", None)
        if not isinstance(chat_payload, dict):
            return []

        direct_messages = chat_payload.get("messages")
        if isinstance(direct_messages, list) and direct_messages:
            return deepcopy(direct_messages)

        history = chat_payload.get("history")
        if not isinstance(history, dict):
            return []

        history_messages = history.get("messages")
        if not isinstance(history_messages, dict) or not history_messages:
            return []

        current_id = history.get("currentId") or history.get("current_id")
        return self._reconstruct_active_history_branch(history_messages, current_id)

    def _get_effective_keep_first(self, messages: List[Dict]) -> int:
        """
        Calculate the index to protect the first N NON-SYSTEM messages.
        All system messages encountered before reaching the Nth non-system message are also kept.
        """
        if not messages or self.valves.keep_first <= 0:
            return 0

        non_system_count = 0

        for i, msg in enumerate(messages):
            if msg.get("role") != "system":
                non_system_count += 1

            if non_system_count >= self.valves.keep_first:
                return i + 1

        # All messages scanned but never reached keep_first non-system messages;
        # protect everything we have.
        return len(messages)

    async def _get_user_context(
        self,
        __user__: Optional[Dict[str, Any]],
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Dict[str, str]:
        """Extract basic user context with safe fallbacks."""
        if isinstance(__user__, (list, tuple)):
            user_data = __user__[0] if __user__ else {}
        elif isinstance(__user__, dict):
            user_data = __user__
        else:
            user_data = {}

        user_language = user_data.get("language", "en-US")

        if __event_call__:
            try:
                js_code = """
                    try {
                        return (
                            document.documentElement.lang ||
                            localStorage.getItem('locale') ||
                            localStorage.getItem('language') ||
                            navigator.language ||
                            'en-US'
                        );
                    } catch (e) {
                        return 'en-US';
                    }
                """
                frontend_lang = await asyncio.wait_for(
                    __event_call__({"type": "execute", "data": {"code": js_code}}),
                    timeout=2.0,
                )
                if frontend_lang and isinstance(frontend_lang, str):
                    user_language = frontend_lang
            except asyncio.TimeoutError:
                logger.warning(
                    "Failed to retrieve frontend language: Timeout (using fallback)"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to retrieve frontend language: {type(e).__name__}: {e}"
                )

        return {
            "user_language": user_language,
        }

    def _parse_model_thresholds(self) -> Dict[str, Any]:
        """Parse model_thresholds string into a dictionary.

        Format: model_id:compression_threshold:max_context, model_id2:threshold2:max2
        Example: gpt-4:8000:32000, claude-3:100000:200000

        Returns cached result if already parsed.
        """
        if self._model_thresholds_cache is not None:
            return self._model_thresholds_cache

        self._model_thresholds_cache = {}
        raw_config = self.valves.model_thresholds
        if not raw_config:
            return self._model_thresholds_cache

        for entry in raw_config.split(","):
            entry = entry.strip()
            if not entry:
                continue

            parts = entry.split(":")
            if len(parts) != 3:
                continue

            try:
                model_id = parts[0].strip()
                compression_threshold = int(parts[1].strip())
                max_context = int(parts[2].strip())

                self._model_thresholds_cache[model_id] = {
                    "compression_threshold_tokens": compression_threshold,
                    "max_context_tokens": max_context,
                }
            except ValueError:
                continue

        return self._model_thresholds_cache

    def _get_model_thresholds(self, model_id: str) -> Dict[str, int]:
        """Gets threshold configuration for a specific model.

        Priority:
        1. If configuration exists for the model ID in model_thresholds,
           use it (direct match or via base_model_id for custom models).
           These are absolute token counts and win over everything else.
        2. Otherwise, the max context is resolved from the model API when
           available (see contextlength.py), falling back to the global
           max_context_tokens valve. The compression threshold is
           compression_threshold_percent of that resolved max context.
        """
        parsed = self._parse_model_thresholds()

        # 1. Direct match with model_id
        if model_id in parsed:
            if self.valves.debug_mode:
                logger.info(f"[Config] Using model-specific configuration: {model_id}")
            return parsed[model_id]

        # 2. Try to find base_model_id for custom models
        model_obj = None
        try:
            model_obj = _call_db_sync(Models.get_model_by_id, model_id)
            if model_obj:
                # Check for base_model_id (custom model)
                base_model_id = getattr(model_obj, "base_model_id", None)
                if not base_model_id:
                    # Try base_model_ids (array) - take first one
                    base_model_ids = getattr(model_obj, "base_model_ids", None)
                    if (
                        base_model_ids
                        and isinstance(base_model_ids, list)
                        and len(base_model_ids) > 0
                    ):
                        base_model_id = base_model_ids[0]

                if base_model_id and base_model_id in parsed:
                    if self.valves.debug_mode:
                        logger.info(
                            f"[Config] Custom model '{model_id}' -> base_model '{base_model_id}': using base model configuration"
                        )
                    return parsed[base_model_id]
        except Exception as e:
            if self.valves.debug_mode:
                logger.warning(
                    f"[Config] Failed to lookup base_model for '{model_id}': {e}"
                )

        # 3. Resolve max context: model-reported value first, global valve
        #    as fallback. The compression threshold is a percentage of
        #    the resolved max context, so it scales with the model.
        max_context_tokens = self.valves.max_context_tokens
        reported = _resolve_reported_context_length(model_id, model_obj)
        if reported is not None:
            max_context_tokens = reported
            if self.valves.debug_mode:
                logger.info(
                    f"[Config] Model '{model_id}' reports context_length={reported}; "
                    f"using it over the global max_context_tokens valve"
                )
        else:
            if self.valves.debug_mode:
                logger.info(
                    f"[Config] Model '{model_id}' not in model_thresholds and no "
                    f"reported context length; using global parameters"
                )

        compression_threshold_tokens = int(
            max_context_tokens * self.valves.compression_threshold_percent / 100
        )

        return {
            "compression_threshold_tokens": compression_threshold_tokens,
            "max_context_tokens": max_context_tokens,
        }

    def _get_summary_model_context_limit(self, model_id: Optional[str]) -> int:
        """Resolve the effective input context window for summary requests."""
        cleaned_model_id = self._clean_model_id(model_id)
        thresholds = (
            self._get_model_thresholds(cleaned_model_id) if cleaned_model_id else {}
        ) or {}

        if self.valves.summary_model_max_context > 0:
            return self.valves.summary_model_max_context

        return thresholds.get("max_context_tokens", self.valves.max_context_tokens)

    async def _self_heal_context_length(self, model_id: str) -> None:
        """Background self-heal for stale context-length snapshots.

        Open WebUI copies the /v1/models payload into the model's meta at
        import time, so a server restarted with a different slot context
        leaves a stale value behind. Re-query the enabled OpenAI-compatible
        connections and rewrite meta.context_length when the server reports
        a different value. Rate-limited per model; all failures are
        swallowed (a background task must never surface an error).
        """
        try:
            if _detect_api_type(model_id) != "openai_api":
                return

            now = time.time()
            last = _context_heal_last_checked.get(model_id)
            if last is not None and now - last < _CONTEXT_HEAL_INTERVAL_SECONDS:
                return
            _context_heal_last_checked[model_id] = now

            row = await _call_db(Models.get_model_by_id, model_id)
            if row is None:
                return

            connections = _get_openai_connections()
            if not connections:
                return

            model_name = model_id.split("/", 1)[1] if "/" in model_id else model_id
            live_value = await _live_query_context_length(connections, model_name)
            if live_value is None:
                return

            stored_value = _resolve_reported_context_length(model_id, row)
            if stored_value == live_value:
                return

            from open_webui.models.models import ModelForm

            new_meta = _meta_to_dict(row.meta)
            if new_meta is None:
                new_meta = {}
            new_meta["context_length"] = live_value
            form = ModelForm(
                id=model_id,
                base_model_id=row.base_model_id,
                name=row.name,
                meta=new_meta,
                params=row.params,
                is_active=row.is_active,
            )
            await _call_db(Models.update_model_by_id, model_id, form)
            await self._log(
                f"[SelfHeal] 🔄 Stored context length for '{model_id}' "
                f"updated: {stored_value} -> {live_value}"
            )
        except Exception as e:
            if self.valves.debug_mode:
                logger.info(f"[SelfHeal] {model_id}: {e}")

    def _extract_system_from_params(self, params: Any) -> Optional[str]:
        """Extract the 'system' prompt from model params (dict, JSON string, or object)."""
        if isinstance(params, str):
            params = json.loads(params)
        elif hasattr(params, "model_dump"):
            params = params.model_dump()
        elif hasattr(params, "dict"):
            params = params.dict()
        if isinstance(params, dict):
            return params.get("system")
        return getattr(params, "system", None)

    def _get_chat_context(
        self, body: dict, __metadata__: Optional[dict] = None
    ) -> Dict[str, str]:
        """
        Unified extraction of chat context information (chat_id).
        Prioritizes extraction from body, then metadata.
        """
        chat_id = ""

        # 1. Try to get from body
        if isinstance(body, dict):
            chat_id = body.get("chat_id", "")

            # Check body.metadata as fallback
            if not chat_id:
                body_metadata = body.get("metadata", {})
                if isinstance(body_metadata, dict):
                    chat_id = body_metadata.get("chat_id", "")

        # 2. Try to get from __metadata__ (as supplement)
        if __metadata__ and isinstance(__metadata__, dict):
            if not chat_id:
                chat_id = __metadata__.get("chat_id", "")

        return {
            "chat_id": str(chat_id).strip(),
        }

    def _should_skip_compression(self, body: dict) -> bool:
        """
        Check if compression should be skipped.
        Returns True if:
        """
        is_copilot = (
            body.get("is_copilot_model", False)
            or body.get("metadata", {}).get("is_copilot_model", False)
            or body.get("features", {}).get("is_copilot_model", False)
        )
        if is_copilot:
            return True

        # Fallback for filters or responses (e.g., Outlet) which may clear the metadata payload
        model_id = body.get("model", "")
        if isinstance(model_id, str):
            c = model_id.lower()
            if (
                "github_copilot_sdk_pipe" in c
                or "github_copilot_official_sdk_pipe" in c
            ):
                return True
        return False

    async def _locked_summary_task(
        self,
        lock: asyncio.Lock,
        chat_id: str,
        model: str,
        body: dict,
        user_data: Optional[dict],
        target_compressed_count: Optional[int],
        lang: str,
        __event_emitter__: Callable,
        __event_call__: Callable,
        __request__: Request = None,
    ):
        """Wrapper to run summary generation with an async lock."""
        async with lock:
            await self._check_and_generate_summary_async(
                chat_id,
                model,
                body,
                user_data,
                target_compressed_count,
                lang,
                __event_emitter__,
                __event_call__,
                __request__,
            )

    async def _check_and_generate_summary_async(
        self,
        chat_id: str,
        model: str,
        body: dict,
        user_data: Optional[dict],
        target_compressed_count: Optional[int],
        lang: str = "en-US",
        __event_emitter__: Callable[[Any], Awaitable[None]] = None,
        __event_call__: Callable[[Any], Awaitable[None]] = None,
        __request__: Request = None,
    ):
        """
        Background processing: Calculates Token count and generates summary (does not block response).
        """

        try:
            messages = body.get("messages", [])

            # Clean model ID
            model = self._clean_model_id(model)

            if self.valves.debug_mode or self.valves.show_debug_log:
                await self._log(
                    f"\n{'='*60}\n[Outlet] Chat ID: {chat_id}\n[Outlet] Response complete\n[Outlet] Full-history compression target: progress={target_compressed_count} | source_messages={len(messages)}",
                    event_call=__event_call__,
                )
                await self._log(
                    f"[Outlet] Background processing started\n{'='*60}\n",
                    event_call=__event_call__,
                )

            # Get threshold configuration for current model
            thresholds = self._get_model_thresholds(model) or {}
            compression_threshold_tokens = thresholds.get(
                "compression_threshold_tokens",
                int(
                    self.valves.max_context_tokens
                    * self.valves.compression_threshold_percent
                    / 100
                ),
            )

            await self._log(
                "\n[🔍 Background Calculation] Starting full-history token count...",
                event_call=__event_call__,
            )

            # --- Fast Estimation Check ---
            # For triggering summary generation, we need to be more precise if we are in the grey zone
            # Margin is 15% (skip tiktoken if estimated is < 85% of threshold)
            # Note: We still use tiktoken if we exceed threshold, because we want an accurate usage status report
            current_tokens, _, used_precise = await self._resolve_context_tokens(
                messages, compression_threshold_tokens
            )
            if used_precise:
                await self._log(
                    "[🔍 Background Calculation] Full-history precise token count\n"
                    f"source_history_tokens={current_tokens}",
                    event_call=__event_call__,
                )
            else:
                await self._log(
                    "[🔍 Background Calculation] Full-history estimate below threshold\n"
                    f"source_history_tokens_est={current_tokens} | compression_threshold_tokens={compression_threshold_tokens} | precise_count_skipped=true",
                    event_call=__event_call__,
                )

            # Send status notification (History Usage format — this count is
            # the saved conversation history only, without the model's system
            # prompt, so it is not directly comparable to the inlet's
            # full-request Context Usage reading). It is also the reading the
            # compaction threshold checks against, so mark it as such.
            max_context_tokens = thresholds.get(
                "max_context_tokens", self.valves.max_context_tokens
            )
            await self._emit_context_usage_status(
                current_tokens,
                max_context_tokens,
                lang,
                __event_emitter__,
                label_key="status_history_usage",
                note_key="status_compaction_drives",
            )

            # Check if compression is needed. A threshold of 0 means the
            # max context is unknown (no valve value, no reported value),
            # in which case we never trigger on the threshold alone.
            if compression_threshold_tokens > 0 and current_tokens >= compression_threshold_tokens:
                await self._log(
                    "[🔍 Background Calculation] ⚡ Full-history threshold triggered\n"
                    f"source_history_tokens={current_tokens} | compression_threshold_tokens={compression_threshold_tokens}",
                    event_call=__event_call__,
                )

                # Proceed to generate summary
                await self._generate_summary_async(
                    messages,
                    chat_id,
                    body,
                    user_data,
                    target_compressed_count,
                    lang,
                    __event_emitter__,
                    __event_call__,
                    __request__,
                )
            else:
                await self._log(
                    "[🔍 Background Calculation] Full-history threshold not reached\n"
                    f"source_history_tokens={current_tokens} | compression_threshold_tokens={compression_threshold_tokens}",
                    event_call=__event_call__,
                )

        except Exception as e:
            await self._log(
                f"[🔍 Background Calculation] ❌ Error: {str(e)}",
                log_type="error",
                event_call=None,
            )
            if __event_call__ and not getattr(e, "_frontend_logged", False):
                await self._emit_frontend_console_log(
                    f"[🔍 Background Calculation] ❌ Error: {str(e)}",
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
            logger.exception("[🔍 Background Calculation] Unhandled exception")
