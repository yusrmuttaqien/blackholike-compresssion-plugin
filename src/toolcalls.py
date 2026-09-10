# ── toolcalls.py · Native tool-call normalization, trimming, atomic grouping 
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


class ToolCallMixin:

    """Native tool-call normalization, trimming, atomic grouping."""

    def _shorten_tool_call_id(self, tool_call_id: str, max_length: int = 40) -> str:
        """Keep tool call IDs within provider limits while staying deterministic."""
        if not isinstance(tool_call_id, str):
            return tool_call_id

        cleaned_id = tool_call_id.strip()
        if len(cleaned_id) <= max_length:
            return cleaned_id

        hash_suffix = hashlib.sha1(cleaned_id.encode("utf-8")).hexdigest()[:8]
        prefix_length = max(0, max_length - len(hash_suffix) - 1)
        return f"{cleaned_id[:prefix_length]}_{hash_suffix}"

    def _normalize_native_tool_call_ids(self, messages: List[Dict]) -> int:
        """Normalize overlong native tool-call IDs and keep assistant/tool links aligned."""
        rewritten_ids: Dict[str, str] = {}

        for message in messages:
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue

                original_id = tool_call.get("id")
                if not isinstance(original_id, str) or not original_id.strip():
                    continue

                normalized_id = rewritten_ids.get(original_id)
                if normalized_id is None:
                    normalized_id = self._shorten_tool_call_id(original_id)
                    rewritten_ids[original_id] = normalized_id

                tool_call["id"] = normalized_id

        if not rewritten_ids:
            return 0

        normalized_count = 0
        for message in messages:
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                continue

            normalized_id = rewritten_ids.get(tool_call_id)
            if normalized_id and normalized_id != tool_call_id:
                message["tool_call_id"] = normalized_id
                normalized_count += 1

        return sum(1 for old_id, new_id in rewritten_ids.items() if old_id != new_id)

    def _trim_native_tool_outputs(
        self, messages: List[Dict], lang: str, collect_debug: bool = False
    ) -> tuple[int, Optional[Dict[str, Any]]]:
        """Collapse verbose native tool outputs while preserving tool-call structure."""
        trimmed_count = 0
        tool_trim_threshold_chars = self.valves.tool_trim_threshold_chars
        collapsed_text = self._get_translation(lang, "content_collapsed").strip()
        atomic_groups = self._get_atomic_groups(messages)
        debug_stats = (
            {
                "threshold_chars": tool_trim_threshold_chars,
                "atomic_groups": len(atomic_groups),
                "native_groups_checked": 0,
                "native_groups_over_threshold": 0,
                "largest_native_group_chars": 0,
                "native_group_samples": [],
                "detail_messages_checked": 0,
                "detail_blocks_found": 0,
                "detail_blocks_over_threshold": 0,
                "largest_detail_result_chars": 0,
                "detail_block_samples": [],
            }
            if collect_debug
            else None
        )

        for group in atomic_groups:
            if len(group) < 2:
                continue

            grouped_messages = [messages[index] for index in group]
            first_message = grouped_messages[0]
            trailing_messages = grouped_messages[1:]

            if not (
                first_message.get("role") == "assistant"
                and first_message.get("tool_calls")
                and trailing_messages
            ):
                continue

            last_message = grouped_messages[-1]
            assistant_followup = None
            tool_messages = trailing_messages

            if (
                len(grouped_messages) >= 3
                and last_message.get("role") == "assistant"
                and all(msg.get("role") == "tool" for msg in grouped_messages[1:-1])
            ):
                assistant_followup = last_message
                tool_messages = grouped_messages[1:-1]
            elif not all(msg.get("role") == "tool" for msg in trailing_messages):
                continue

            tool_chars = sum(len(str(msg.get("content", ""))) for msg in tool_messages)
            if debug_stats is not None:
                debug_stats["native_groups_checked"] += 1
                debug_stats["largest_native_group_chars"] = max(
                    debug_stats["largest_native_group_chars"], tool_chars
                )
                if len(debug_stats["native_group_samples"]) < 5:
                    debug_stats["native_group_samples"].append(
                        {
                            "group_size": len(grouped_messages),
                            "tool_count": len(tool_messages),
                            "tool_chars": tool_chars,
                            "trimmed": tool_chars >= tool_trim_threshold_chars,
                        }
                    )

            if tool_chars < tool_trim_threshold_chars:
                continue
            if debug_stats is not None:
                debug_stats["native_groups_over_threshold"] += 1

            for tool_message in tool_messages:
                metadata = tool_message.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["is_trimmed"] = True
                metadata["trimmed_by"] = SUMMARY_METADATA_SOURCE
                tool_message["metadata"] = metadata
                tool_message["content"] = collapsed_text
                trimmed_count += 1

            if assistant_followup is not None:
                final_content = assistant_followup.get("content", "")
                if isinstance(final_content, str) and final_content.strip():
                    assistant_metadata = assistant_followup.get("metadata", {})
                    if not isinstance(assistant_metadata, dict):
                        assistant_metadata = {}
                    if not assistant_metadata.get("tool_outputs_trimmed"):
                        assistant_followup["content"] = self._get_translation(
                            lang, "tool_trimmed", content=final_content
                        )
                        assistant_metadata["tool_outputs_trimmed"] = True
                        assistant_metadata["trimmed_by"] = SUMMARY_METADATA_SOURCE
                        assistant_followup["metadata"] = assistant_metadata

        for message in messages:
            content = message.get("content", "")
            if (
                not isinstance(content, str)
                or '<details type="tool_calls"' not in content
            ):
                continue

            trimmed_blocks = 0
            if debug_stats is not None:
                debug_stats["detail_messages_checked"] += 1

            def _replace_tool_block(match: re.Match) -> str:
                nonlocal trimmed_blocks
                block = match.group(0)
                result_match = re.search(r'result="([^"]*)"', block)

                if not result_match:
                    return block

                result_chars = len(result_match.group(1))
                if debug_stats is not None:
                    debug_stats["detail_blocks_found"] += 1
                    debug_stats["largest_detail_result_chars"] = max(
                        debug_stats["largest_detail_result_chars"], result_chars
                    )
                    if len(debug_stats["detail_block_samples"]) < 5:
                        debug_stats["detail_block_samples"].append(result_chars)

                if result_chars < tool_trim_threshold_chars:
                    return block

                if debug_stats is not None:
                    debug_stats["detail_blocks_over_threshold"] += 1
                trimmed_blocks += 1
                return re.sub(
                    r'result="([^"]*)"',
                    f'result="&quot;{collapsed_text}&quot;"',
                    block,
                    count=1,
                )

            new_content = re.sub(
                r'<details type="tool_calls"[\s\S]*?</details>',
                _replace_tool_block,
                content,
            )

            if trimmed_blocks <= 0:
                continue

            metadata = message.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["tool_outputs_trimmed"] = True
            metadata["trimmed_by"] = SUMMARY_METADATA_SOURCE
            message["metadata"] = metadata
            message["content"] = new_content
            trimmed_count += trimmed_blocks

        return trimmed_count, debug_stats

    def _get_atomic_groups(self, messages: List[Dict]) -> List[List[int]]:
        """
        Groups message indices into atomic units that must be kept or dropped together.
        Specifically handles native tool-calling sequences:
        - assistant(tool_calls)
        - tool(s)
        - assistant(final response)
        """
        groups = []
        current_group = []

        for i, msg in enumerate(messages):
            role = msg.get("role")
            has_tool_calls = bool(msg.get("tool_calls"))

            # Logic:
            # 1. If assistant message has tool_calls, it starts a potential block.
            # 2. If message is 'tool' role, it MUST belong to the preceding assistant group.
            # 3. If message is 'assistant' and follows a 'tool' group, it's the final answer.

            if role == "assistant" and has_tool_calls:
                # Close previous group if any
                if current_group:
                    groups.append(current_group)
                current_group = [i]
            elif role == "tool":
                # Force tool results into the current group
                if not current_group:
                    # An orphaned tool result? Group it alone but warn
                    groups.append([i])
                else:
                    current_group.append(i)
            elif (
                role == "assistant"
                and current_group
                and messages[current_group[-1]].get("role") == "tool"
            ):
                # This is likely the assistant follow-up consuming tool results
                current_group.append(i)
                groups.append(current_group)
                current_group = []
            else:
                # Regular message (user, or assistant without tool calls)
                if current_group:
                    groups.append(current_group)
                    current_group = []
                groups.append([i])

        if current_group:
            groups.append(current_group)

        return groups

    def _align_tail_start_to_atomic_boundary(
        self, messages: List[Dict], raw_start_index: int, protected_prefix: int
    ) -> int:
        """
        Align the retained tail to an atomic-group boundary.

        If the raw tail start falls in the middle of an assistant/tool/assistant
        chain, move it backward to the start of that chain so the next request
        never begins with an orphaned tool result or assistant follow-up.
        """
        aligned_start = max(raw_start_index, protected_prefix)

        if aligned_start <= protected_prefix or aligned_start >= len(messages):
            return aligned_start

        trimmable = messages[protected_prefix:]
        local_start = aligned_start - protected_prefix

        for group in self._get_atomic_groups(trimmable):
            group_start = group[0]
            group_end = group[-1] + 1

            if local_start == group_start:
                return aligned_start

            if group_start < local_start < group_end:
                return protected_prefix + group_start

        return aligned_start

    def _infer_native_function_calling_from_messages(self, messages: Any) -> bool:
        """Infer native function-calling mode from tool-shaped messages."""
        if not isinstance(messages, list):
            return False

        for message in messages:
            if not isinstance(message, dict):
                continue

            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                return True

            if message.get("role") == "tool":
                return True

            content = message.get("content", "")
            if isinstance(content, str) and '<details type="tool_calls"' in content:
                return True

        return False

    def _unfold_messages(self, messages: Any) -> List[Dict[str, Any]]:
        """
        Reverse-expand compact UI messages back into their native tool-calling sequence
        by parsing the hidden 'output' dictionary, identical to what OpenWebUI does
        in the inlet phase (middleware.py:process_messages_with_output).
        """
        if not isinstance(messages, list):
            return messages

        unfolded = []
        for msg in messages:
            if not isinstance(msg, dict):
                unfolded.append(msg)
                continue

            # If it's an assistant message with the hidden 'output' field, unfold it
            if (
                msg.get("role") == "assistant"
                and isinstance(msg.get("output"), list)
                and msg.get("output")
            ):
                try:
                    from open_webui.utils.misc import convert_output_to_messages

                    expanded = convert_output_to_messages(msg["output"], raw=True)
                    if expanded:
                        expanded_has_tool_structure = any(
                            isinstance(expanded_msg, dict)
                            and (
                                expanded_msg.get("role") == "tool"
                                or bool(expanded_msg.get("tool_calls"))
                            )
                            for expanded_msg in expanded
                        )
                        expanded_chars = sum(
                            self._message_content_char_length(
                                expanded_msg.get("content", "")
                            )
                            for expanded_msg in expanded
                            if isinstance(expanded_msg, dict)
                        )
                        original_chars = self._message_content_char_length(
                            msg.get("content", "")
                        )

                        if (
                            expanded_has_tool_structure
                            or expanded_chars > original_chars
                        ):
                            unfolded.extend(expanded)
                            continue
                except ImportError:
                    pass  # Fallback if for some reason the internal import fails

            # Clean message (strip 'output' field just like inlet does)
            clean_msg = {k: v for k, v in msg.items() if k != "output"}
            unfolded.append(clean_msg)

        return unfolded

    def _get_function_calling_mode(self, body: dict) -> str:
        """Read function-calling mode from all known OpenWebUI payload locations."""
        metadata = body.get("metadata", {}) if isinstance(body, dict) else {}
        params = body.get("params", {}) if isinstance(body, dict) else {}
        messages = body.get("messages", []) if isinstance(body, dict) else []

        if isinstance(metadata, dict):
            mode = metadata.get("function_calling")
            if isinstance(mode, str) and mode.strip():
                return mode.strip()

        if isinstance(params, dict):
            mode = params.get("function_calling")
            if isinstance(mode, str) and mode.strip():
                return mode.strip()

        if self._infer_native_function_calling_from_messages(messages):
            return "native"

        return ""
