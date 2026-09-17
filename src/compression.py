# ── compression.py · History reconstruction, thresholds, compression orchestration 
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


class CompressionMixin:

    """History reconstruction, thresholds, compression orchestration."""

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create an asyncio lock for a specific chat ID."""
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

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
            # Bound memory: the dict is insertion-ordered, so evict the oldest
            # chats once we exceed the cap.
            while len(self._pending_inlet_messages) > PENDING_INLET_MAX_CHATS:
                oldest_chat_id = next(iter(self._pending_inlet_messages))
                self._pending_inlet_messages.pop(oldest_chat_id, None)
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

        user_id = user_data.get("id", "unknown_user")
        user_name = user_data.get("name", "User")
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
            "user_id": user_id,
            "user_name": user_name,
            "user_language": user_language,
        }

    def _extract_model_context_length(
        self, model_dict: Optional[dict]
    ) -> Optional[int]:
        """Pull context_length out of a model dict, tolerating both shapes.

        Accepts either the filter's ``__model__`` dict or
        ``body["metadata"]["model"]``; both expose metadata at
        ``["info"]["meta"]``, while a DB model dump flattens it to ``["meta"]``.
        Returns None when the value is missing or not a positive number.
        """
        if not isinstance(model_dict, dict):
            return None

        meta = None
        info = model_dict.get("info")
        if isinstance(info, dict):
            meta = info.get("meta")
        if not isinstance(meta, dict):
            meta = model_dict.get("meta")
        if not isinstance(meta, dict):
            return None

        value = meta.get("context_length")
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        return None

    async def _get_model_max_context(
        self, model_id: str, model_dict: Optional[dict] = None
    ) -> int:
        """Resolve the active model's max context window, in tokens.

        Priority:
        1. ``model_dict`` metadata (``["info"]["meta"]["context_length"]``)
           — fast, no DB hit. ``model_dict`` is the filter's ``__model__`` or
           ``body["metadata"]["model"]``.
        2. Database lookup via ``Models.get_model_by_id``.
        3. llama.cpp server probe (``GET /props`` / ``/v1/models``) when the
           active model is served through a llama.cpp connection.
        4. Fallback to the global ``max_context_tokens`` valve (0 = no limit).
        """
        resolved = self._extract_model_context_length(model_dict)
        if resolved:
            if self.valves.debug_mode:
                logger.info(
                    f"[Config] Model '{model_id}': using declared context_length={resolved}"
                )
            return resolved

        try:
            model_obj = await _call_db(Models.get_model_by_id, model_id)
            if model_obj is not None:
                meta = getattr(model_obj, "meta", None)
                if hasattr(meta, "model_dump"):
                    meta = meta.model_dump()
                resolved = self._extract_model_context_length({"meta": meta})
                if resolved:
                    if self.valves.debug_mode:
                        logger.info(
                            f"[Config] Model '{model_id}': context_length={resolved} (from DB)"
                        )
                    return resolved
        except Exception as e:
            if self.valves.debug_mode:
                logger.warning(
                    f"[Config] Failed to resolve context_length for '{model_id}': {e}"
                )

        if self.valves.enable_llamacpp_context_probe:
            resolved = await self._get_llamacpp_context(model_id, model_dict)
            if resolved:
                if self.valves.debug_mode:
                    logger.info(
                        f"[Config] Model '{model_id}': context_length={resolved} "
                        f"(from llama.cpp server probe)"
                    )
                return resolved

        if self.valves.debug_mode:
            logger.info(
                f"[Config] Model '{model_id}' has no declared context_length; "
                f"using fallback max_context_tokens={self.valves.max_context_tokens}"
            )
        return self.valves.max_context_tokens

    def _get_compression_threshold(self, max_context_tokens: int) -> int:
        """Compute the compression trigger from the max context window.

        Returns ``compression_threshold_percent``% of ``max_context_tokens``,
        or 0 when the window is unlimited/unknown (``max_context_tokens <= 0``),
        in which case compression is skipped.
        """
        if max_context_tokens <= 0:
            return 0
        return int(
            max_context_tokens * self.valves.compression_threshold_percent / 100
        )

    # ── llama.cpp context auto-detection ──────────────────────────────
    # Open WebUI exposes no context length for OpenAI-compatible connections,
    # so for llama.cpp we ask the server itself (GET /props → runtime n_ctx,
    # GET /v1/models → n_ctx_train fallback) and cache the answer briefly.

    @staticmethod
    def _extract_llamacpp_n_ctx(payload: Optional[dict]) -> Optional[int]:
        """Read the runtime context size from a llama.cpp ``/props`` response."""
        if not isinstance(payload, dict):
            return None
        for container in (payload.get("default_generation_settings"), payload):
            if isinstance(container, dict):
                value = container.get("n_ctx")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return None

    @staticmethod
    def _extract_llamacpp_n_ctx_train(
        payload: Optional[dict], model_id: Optional[str] = None
    ) -> Optional[int]:
        """Read ``n_ctx_train`` from a llama.cpp ``/v1/models`` response."""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, list):
            return None
        entries = [e for e in data if isinstance(e, dict)]
        preferred = [e for e in entries if model_id and e.get("id") == model_id]
        for entry in preferred or entries:
            meta = entry.get("meta")
            if isinstance(meta, dict):
                value = meta.get("n_ctx_train")
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return None

    @staticmethod
    def _llamacpp_root_url(url: Optional[str]) -> Optional[str]:
        """Strip the OpenAI API suffix from a connection URL."""
        if not isinstance(url, str) or not url.strip():
            return None
        root = url.strip().rstrip("/")
        for suffix in ("/api/v1", "/api/v0", "/v1"):
            if root.endswith(suffix):
                return root[: -len(suffix)]
        return root

    def _find_model_dict(self, model_id: Optional[str]) -> Optional[dict]:
        """Look up a model dict in Open WebUI's global model registry."""
        if not model_id:
            return None
        state = getattr(webui_app, "state", None)
        for attr in ("MODELS", "OPENAI_MODELS"):
            registry = getattr(state, attr, None)
            if not registry:
                continue
            try:
                found = registry.get(model_id)
            except Exception:
                found = None
            if isinstance(found, dict):
                return found
        return None

    async def _get_openai_connection_config(self, url_idx: int) -> tuple:
        """Resolve an Open WebUI OpenAI-compatible connection by index.

        Returns ``(base_url, api_key, api_config)`` or ``(None, None, {})``.
        Supports both the legacy ``app.state.config`` (Open WebUI <= 0.9.x)
        and the config table used by newer releases.
        """
        # Open WebUI <= 0.9.x keeps connections on app.state.config
        cfg = getattr(getattr(webui_app, "state", None), "config", None)
        urls = getattr(cfg, "OPENAI_API_BASE_URLS", None)
        if isinstance(urls, (list, tuple)) and 0 <= url_idx < len(urls):
            keys = getattr(cfg, "OPENAI_API_KEYS", None) or []
            configs = getattr(cfg, "OPENAI_API_CONFIGS", None) or {}
            url = urls[url_idx]
            key = keys[url_idx] if url_idx < len(keys) else ""
            api_config = configs.get(str(url_idx), configs.get(url, {})) or {}
            return url, key, api_config

        if OWUIConfig is not None:
            try:
                values = await OWUIConfig.get_many(
                    "openai.api_base_urls",
                    "openai.api_keys",
                    "openai.api_configs",
                )
                urls = values.get("openai.api_base_urls") or []
                if 0 <= url_idx < len(urls):
                    keys = values.get("openai.api_keys") or []
                    configs = values.get("openai.api_configs") or {}
                    url = urls[url_idx]
                    key = keys[url_idx] if url_idx < len(keys) else ""
                    api_config = configs.get(str(url_idx), configs.get(url, {})) or {}
                    return url, key, api_config
            except Exception as e:
                if self.valves.debug_mode:
                    logger.warning(
                        f"[Config] Failed to read OpenAI connection config: {e}"
                    )
        return None, None, {}

    async def _probe_llamacpp_context(
        self,
        root_url: str,
        api_key: Optional[str],
        raw_model_id: Optional[str],
        model_id: Optional[str],
    ) -> Optional[int]:
        """Query a llama.cpp server for the active model's context size."""
        if aiohttp is None:
            return None

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1. /props reports the runtime context size (--ctx-size).
                param_sets = [None]
                if raw_model_id:
                    param_sets.append({"model": raw_model_id})
                for params in param_sets:
                    try:
                        async with session.get(
                            f"{root_url}/props", headers=headers, params=params
                        ) as resp:
                            if resp.status == 200:
                                resolved = self._extract_llamacpp_n_ctx(
                                    await resp.json(content_type=None)
                                )
                                if resolved:
                                    return resolved
                    except Exception:
                        continue

                # 2. /v1/models exposes the trained context size as a fallback.
                try:
                    async with session.get(
                        f"{root_url}/v1/models", headers=headers
                    ) as resp:
                        if resp.status == 200:
                            return self._extract_llamacpp_n_ctx_train(
                                await resp.json(content_type=None),
                                raw_model_id or model_id,
                            )
                except Exception:
                    pass
        except Exception as e:
            if self.valves.debug_mode:
                logger.warning(f"[Config] llama.cpp probe failed for {root_url}: {e}")
        return None

    async def _get_llamacpp_context(
        self, model_id: Optional[str], model_dict: Optional[dict] = None
    ) -> Optional[int]:
        """Resolve a llama.cpp model's context window from its server.

        Only runs when the active model belongs to a llama.cpp connection.
        Results (including misses) are cached to keep the request path cheap.
        """
        model_dict = (
            model_dict
            if isinstance(model_dict, dict)
            else self._find_model_dict(model_id)
        )
        if not isinstance(model_dict, dict):
            return None

        # Resolve the connection index (custom models carry no urlIdx).
        url_idx = model_dict.get("urlIdx")
        raw_model = model_dict.get("openai")
        raw_model_id = raw_model.get("id") if isinstance(raw_model, dict) else None
        if not isinstance(url_idx, int):
            info = model_dict.get("info")
            base_id = info.get("base_model_id") if isinstance(info, dict) else None
            base_dict = self._find_model_dict(base_id)
            if isinstance(base_dict, dict):
                url_idx = base_dict.get("urlIdx")
                if raw_model is None:
                    raw_model = base_dict.get("openai")
                    raw_model_id = (
                        raw_model.get("id") if isinstance(raw_model, dict) else None
                    )
        if not isinstance(url_idx, int):
            return None

        base_url, api_key, api_config = await self._get_openai_connection_config(
            url_idx
        )
        root_url = self._llamacpp_root_url(base_url)
        if not root_url:
            return None

        provider = (api_config or {}).get("provider") or model_dict.get("provider") or ""
        owned_by = raw_model.get("owned_by") if isinstance(raw_model, dict) else ""
        if provider != "llama.cpp" and owned_by != "llamacpp":
            return None

        cache_key = f"{root_url}|{raw_model_id or model_id}"
        now = time.time()
        cached = self._llamacpp_context_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        resolved = await self._probe_llamacpp_context(
            root_url, api_key, raw_model_id, model_id
        )
        # Cache hits for 5 minutes, misses for 1 to avoid hammering the server.
        self._llamacpp_context_cache[cache_key] = (
            now + (300 if resolved else 60),
            resolved,
        )
        return resolved

    async def _get_summary_model_context_limit(self, model_id: Optional[str]) -> int:
        """Resolve the effective input context window for summary requests."""
        if self.valves.summary_model_max_context > 0:
            return self.valves.summary_model_max_context

        cleaned_model_id = self._clean_model_id(model_id)
        if not cleaned_model_id:
            return self.valves.max_context_tokens

        return await self._get_model_max_context(cleaned_model_id)

    def _get_chat_context(
        self, body: dict, __metadata__: Optional[dict] = None
    ) -> Dict[str, str]:
        """
        Unified extraction of chat context information (chat_id, message_id).
        Prioritizes extraction from body, then metadata.
        """
        chat_id = ""
        message_id = ""

        # 1. Try to get from body
        if isinstance(body, dict):
            chat_id = body.get("chat_id", "")
            message_id = body.get("id", "")  # message_id is usually 'id' in body

            # Check body.metadata as fallback
            if not chat_id or not message_id:
                body_metadata = body.get("metadata", {})
                if isinstance(body_metadata, dict):
                    if not chat_id:
                        chat_id = body_metadata.get("chat_id", "")
                    if not message_id:
                        message_id = body_metadata.get("message_id", "")

        # 2. Try to get from __metadata__ (as supplement)
        if __metadata__ and isinstance(__metadata__, dict):
            if not chat_id:
                chat_id = __metadata__.get("chat_id", "")
            if not message_id:
                message_id = __metadata__.get("message_id", "")

        return {
            "chat_id": str(chat_id).strip(),
            "message_id": str(message_id).strip(),
        }

    def _should_skip_compression(
        self, body: dict, __model__: Optional[dict] = None
    ) -> bool:
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
            try:
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
            finally:
                # Drop the per-chat lock while still holding it so the map stays
                # bounded. The identity check avoids removing a lock that a newer
                # task may have already installed in its place.
                if self._chat_locks.get(chat_id) is lock:
                    self._chat_locks.pop(chat_id, None)

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

            # Resolve the active model's context window and derive the trigger
            model_meta = (body.get("metadata") or {}).get("model")
            max_context_tokens = await self._get_model_max_context(model, model_meta)
            compression_threshold_tokens = self._get_compression_threshold(
                max_context_tokens
            )

            await self._log(
                "\n[🔍 Background Calculation] Starting full-history token count...",
                event_call=__event_call__,
            )

            # --- Fast Estimation Check ---
            estimated_tokens = self._estimate_messages_tokens(messages)

            # For triggering summary generation, we need to be more precise if we are in the grey zone
            # Margin is 15% (skip tiktoken if estimated is < 85% of threshold)
            # Note: We still use tiktoken if we exceed threshold, because we want an accurate usage status report
            if estimated_tokens < compression_threshold_tokens * 0.85:
                current_tokens = estimated_tokens
                await self._log(
                    "[🔍 Background Calculation] Full-history estimate below threshold\n"
                    f"source_history_tokens_est={current_tokens} | compression_threshold_tokens={compression_threshold_tokens} | precise_count_skipped=true",
                    event_call=__event_call__,
                )
            else:
                # Calculate Token count precisely in a background thread
                current_tokens = await asyncio.to_thread(
                    self._calculate_messages_tokens, messages
                )
                await self._log(
                    "[🔍 Background Calculation] Full-history precise token count\n"
                    f"source_history_tokens={current_tokens}",
                    event_call=__event_call__,
                )

            # Send status notification (Context Usage format)
            if __event_emitter__:
                if max_context_tokens > 0:
                    usage_ratio = current_tokens / max_context_tokens
                    # Only show status if threshold is met
                    if self._should_show_status(usage_ratio):
                        status_msg = self._get_translation(
                            lang,
                            "status_context_usage",
                            tokens=current_tokens,
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

            # Check if compression is needed
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
