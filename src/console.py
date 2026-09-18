# ── console.py · Frontend console logging + status ────────────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


class ConsoleMixin:

    """Frontend console logging + status."""

    async def _emit_frontend_console_log(
        self,
        message: str,
        log_type: str = "info",
        event_call=None,
        force: bool = False,
    ):
        """Emit a browser-console log, optionally bypassing the debug-log valve."""
        if not event_call:
            return
        if not force and (
            not self.valves.show_debug_log or self._frontend_broadcast_broken
        ):
            return

        try:
            css = "color: #3b82f6;"
            console_method = "log"
            if log_type == "error":
                css = "color: #ef4444; font-weight: bold;"
                console_method = "error"
            elif log_type == "warning":
                css = "color: #f59e0b;"
                console_method = "warn"
            elif log_type == "success":
                css = "color: #10b981; font-weight: bold;"

            lines = message.split("\n")
            filtered_lines = [
                line
                for line in lines
                if not line.strip().startswith("====")
                and not line.strip().startswith("----")
            ]
            clean_message = "\n".join(filtered_lines).strip()

            if not clean_message:
                return

            message_lines = [
                line.rstrip() for line in clean_message.split("\n") if line.strip()
            ]
            header_line = message_lines[0] if message_lines else clean_message
            detail_lines = message_lines[1:] if len(message_lines) > 1 else []

            if detail_lines:
                js_code = f"""
                    try {{
                        const header = {json.dumps("[Compression] " + header_line, ensure_ascii=False)};
                        const detailLines = {json.dumps(detail_lines, ensure_ascii=False)};
                        console.groupCollapsed("%c" + header, "{css}");
                        for (const line of detailLines) {{
                            console.{console_method}(line);
                        }}
                        console.groupEnd();
                        return true;
                    }} catch (e) {{
                        console.error("[Compression] Failed to emit console log", e);
                        return false;
                    }}
                """
            else:
                js_code = f"""
                    try {{
                        console.{console_method}("%c" + {json.dumps("[Compression] " + header_line, ensure_ascii=False)}, "{css}");
                        return true;
                    }} catch (e) {{
                        console.error("[Compression] Failed to emit console log", e);
                        return false;
                    }}
                """

            await asyncio.wait_for(
                event_call({"type": "execute", "data": {"code": js_code}}),
                timeout=2.0,
            )
        except ValueError as ve:
            if "broadcast" in str(ve).lower():
                logger.debug(
                    "Cannot broadcast to frontend without explicit room; suppressing further frontend logs in this session."
                )
                if not force:
                    self._frontend_broadcast_broken = True
            else:
                logger.error(f"Failed to process log to frontend: ValueError: {ve}")
        except Exception as e:
            logger.error(f"Failed to process log to frontend: {type(e).__name__}: {e}")

    async def _log(self, message: str, log_type: str = "info", event_call=None):
        """Unified logging to both backend (print) and frontend (console.log)"""
        # Backend logging
        if self.valves.debug_mode:
            logger.info(message)

        await self._emit_frontend_console_log(
            message, log_type=log_type, event_call=event_call
        )

    def _should_show_status(self, usage_ratio: float) -> bool:
        """
        Check if token usage status should be shown based on threshold.

        Args:
            usage_ratio: Current usage ratio (0.0 to 1.0)

        Returns:
            True if status should be shown, False otherwise
        """
        if not self.valves.show_token_usage_status:
            return False

        # If threshold is 0, always show
        if self.valves.token_usage_status_threshold == 0:
            return True

        # Check if usage exceeds threshold
        threshold_ratio = self.valves.token_usage_status_threshold / 100.0
        return usage_ratio >= threshold_ratio

    async def _emit_context_usage_status(
        self,
        tokens: int,
        max_context_tokens: int,
        lang: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> None:
        """Emit the 'Context Usage' status notification if the usage ratio warrants it."""
        if not __event_emitter__ or max_context_tokens <= 0:
            return
        usage_ratio = tokens / max_context_tokens
        if not self._should_show_status(usage_ratio):
            return
        status_msg = self._get_translation(
            lang,
            "status_context_usage",
            tokens=tokens,
            max_tokens=max_context_tokens,
            ratio=f"{usage_ratio * 100:.1f}",
        )
        if usage_ratio > 0.9:
            status_msg += self._get_translation(lang, "status_high_usage")
        await __event_emitter__(
            {"type": "status", "data": {"description": status_msg, "done": True}}
        )
