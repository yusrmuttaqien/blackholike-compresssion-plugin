# ── externalrefs.py · Cross-chat reference loading/injection ──────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


class ExternalRefsMixin:

    """Cross-chat reference loading/injection."""

    async def _handle_external_chat_references(
        self,
        body: dict,
        user_data: Optional[dict] = None,
        __event_call__: Callable = None,
        __request__: Request = None,
    ) -> dict:
        metadata = body.get("metadata", {})
        files = metadata.get("files", [])

        if not files:
            return body

        chat_files = [f for f in files if f.get("type") == "chat"]
        if not chat_files:
            return body

        if __event_call__:
            await self._log(
                f"[Inlet] 📎 Found {len(chat_files)} external chat reference(s)",
                event_call=__event_call__,
            )

        model_id = self._clean_model_id(body.get("model"))
        thresholds = self._get_model_thresholds(model_id) or {}
        max_context_tokens = thresholds.get(
            "max_context_tokens", self.valves.max_context_tokens
        )
        max_summary_tokens = self.valves.max_summary_tokens or 4096
        summary_model = (
            self._clean_model_id(self.valves.summary_model)
            or self._clean_model_id(body.get("model"))
            or "gpt-4o-mini"
        )
        summary_model_max_context = self._get_summary_model_context_limit(summary_model)

        base_messages = body.get("messages", [])
        base_message_tokens = self._estimate_messages_tokens(base_messages)
        remaining_direct_budget = (
            max(0, max_context_tokens - base_message_tokens)
            if max_context_tokens and max_context_tokens > 0
            else max_summary_tokens
        )

        referenced_summaries = []
        for chat_file in chat_files:
            ref_chat_id = chat_file.get("id")
            if isinstance(ref_chat_id, str):
                ref_chat_title = chat_file.get("name", f"Chat {ref_chat_id[:8]}...")
            else:
                ref_chat_title = chat_file.get("name", "Unknown Chat")

            if not ref_chat_id:
                continue

            summary_record = await self._load_summary_record(ref_chat_id)

            if summary_record and summary_record.summary:
                remaining_direct_budget = max(
                    0,
                    remaining_direct_budget
                    - _estimate_text_tokens(summary_record.summary),
                )
                referenced_summaries.append(
                    {
                        "chat_id": ref_chat_id,
                        "title": ref_chat_title,
                        "summary": summary_record.summary,
                        "type": "existing",
                    }
                )
                if __event_call__:
                    await self._log(
                        f"[Inlet] ✅ Found existing summary for referenced chat '{ref_chat_title}' ({len(summary_record.summary)} chars)",
                        event_call=__event_call__,
                    )
            else:
                chat_messages = await self._load_full_chat_messages(ref_chat_id)
                if not chat_messages:
                    if __event_call__:
                        await self._log(
                            f"[Inlet] ⚠️ No messages found for '{ref_chat_title}', skipping",
                            event_call=__event_call__,
                        )
                    continue

                conversation_text = self._format_messages_for_summary(chat_messages)
                estimated_tokens = _estimate_text_tokens(conversation_text)
                inject_full_chat = estimated_tokens <= max(0, remaining_direct_budget)

                if inject_full_chat:
                    referenced_summaries.append(
                        {
                            "chat_id": ref_chat_id,
                            "title": ref_chat_title,
                            "summary": conversation_text,
                            "type": "full",
                        }
                    )
                    remaining_direct_budget = max(
                        0, remaining_direct_budget - estimated_tokens
                    )
                    if __event_call__:
                        await self._log(
                            f"[Inlet] 📄 Chat '{ref_chat_title}' fits current model budget ({estimated_tokens} tokens), injecting full content",
                            event_call=__event_call__,
                        )
                else:
                    summary_input_text = conversation_text
                    covered_message_count = len(chat_messages)
                    covers_full_history = True

                    if (
                        summary_model_max_context > 0
                        and estimated_tokens > summary_model_max_context
                    ):
                        summary_input_text = self._truncate_messages_for_summary(
                            chat_messages, summary_model_max_context
                        )
                        truncated_tokens = _estimate_text_tokens(summary_input_text)
                        covered_message_count = 0
                        covers_full_history = False
                        if __event_call__:
                            await self._log(
                                f"[Inlet] ✂️ Chat '{ref_chat_title}' exceeds summary input budget, truncating recent window from {estimated_tokens} to {truncated_tokens} tokens before summarization",
                                event_call=__event_call__,
                            )

                    summary = ""
                    generated_with_llm = False

                    if isinstance(user_data, dict) and user_data.get("id"):
                        if __event_call__:
                            await self._log(
                                f"[Inlet] 🤖 Generating referenced chat summary for '{ref_chat_title}' with model '{summary_model}'",
                                event_call=__event_call__,
                            )
                        try:
                            summary = await self._call_summary_llm(
                                summary_input_text,
                                {"model": summary_model},
                                user_data,
                                __event_call__,
                                __request__,
                                previous_summary=None,
                            )
                            generated_with_llm = bool(summary)
                        except Exception as exc:
                            logger.warning(
                                "[Inlet] Referenced chat summary failed for '%s': %s",
                                ref_chat_title,
                                exc,
                            )
                            if __event_call__:
                                await self._log(
                                    f"[Inlet] ⚠️ Referenced chat summary failed for '{ref_chat_title}', falling back to direct contextual injection: {exc}",
                                    log_type="warning",
                                    event_call=__event_call__,
                                )
                    else:
                        if __event_call__:
                            await self._log(
                                f"[Inlet] ⚠️ Missing user context for '{ref_chat_title}', falling back to direct contextual injection without LLM summary",
                                event_call=__event_call__,
                            )

                    if not summary:
                        summary = summary_input_text
                        if __event_call__:
                            await self._log(
                                f"[Inlet] 📎 Falling back to direct contextual injection for '{ref_chat_title}'",
                                event_call=__event_call__,
                            )

                    summary_estimate = _estimate_text_tokens(summary)
                    if summary_estimate > max_summary_tokens:
                        target_chars = max(
                            1, int(len(summary) * max_summary_tokens / summary_estimate)
                        )
                        summary = summary[:target_chars]
                        if __event_call__:
                            await self._log(
                                f"[Inlet] ✂️ Trimmed injected context for '{ref_chat_title}' to stay near {max_summary_tokens} tokens",
                                event_call=__event_call__,
                            )
                        summary_estimate = _estimate_text_tokens(summary)

                    remaining_direct_budget = max(
                        0, remaining_direct_budget - summary_estimate
                    )

                    referenced_summaries.append(
                        {
                            "chat_id": ref_chat_id,
                            "title": ref_chat_title,
                            "summary": summary,
                            "type": (
                                "generated_summary"
                                if generated_with_llm
                                else "direct_fallback"
                            ),
                        }
                    )

                    if (
                        generated_with_llm
                        and covers_full_history
                        and covered_message_count > 0
                    ):
                        await self._save_summary(
                            ref_chat_id,
                            summary,
                            covered_message_count,
                        )
                        if __event_call__:
                            await self._log(
                                f"[Inlet] 💾 Saved summary cache for '{ref_chat_title}'",
                                event_call=__event_call__,
                            )

        if not referenced_summaries:
            return body

        summary_parts = []
        for ref in referenced_summaries:
            summary_parts.append(
                f'<referenced_chat id="{ref["chat_id"]}" name="{ref["title"]}">\n{ref["summary"]}\n</referenced_chat>'
            )

        if summary_parts:
            ref_context = "\n\n".join(summary_parts)
            ref_content = f"<referenced_chats>\n{ref_context}\n</referenced_chats>"

            body["__external_references__"] = {
                "content": ref_content,
                "references": [
                    {"chat_id": ref["chat_id"], "title": ref["title"]}
                    for ref in referenced_summaries
                ],
            }

            if __event_call__:
                await self._log(
                    f"[Inlet] 💉 Prepared {len(referenced_summaries)} referenced chat context block(s) for injection",
                    event_call=__event_call__,
                )

        return body
