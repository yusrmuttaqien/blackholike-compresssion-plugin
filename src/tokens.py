# ── tokens.py · Token counting (tiktoken + fast estimator) ────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


# Global cache for tiktoken encoding
TIKTOKEN_ENCODING = None
if tiktoken:
    try:
        TIKTOKEN_ENCODING = tiktoken.get_encoding("o200k_base")
    except Exception as e:
        logger.error(f"[Init] Failed to load tiktoken encoding: {e}")


ASCII_PUNCTUATION_CHARS = ".,:;!?/\\()[]{}<>-=+*_`"
SCRIPT_BYTE_COEFFICIENTS = {
    "han": 0.295,
    "kana": 0.235,
    "hangul": 0.175,
    "cyr": 0.13,
    "arabic": 0.135,
    "thai": 0.145,
    "other": 0.22,
}


def _sample_script_mix(text: str, limit: int = 256) -> tuple[Dict[str, int], str]:
    counts = {key: 0 for key in SCRIPT_BYTE_COEFFICIENTS}
    seen = 0

    for char in text:
        codepoint = ord(char)
        if codepoint < 128:
            continue

        seen += 1
        if 0x3040 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
            counts["kana"] += 1
        elif (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            counts["han"] += 1
        elif (
            0x1100 <= codepoint <= 0x11FF
            or 0x3130 <= codepoint <= 0x318F
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            counts["hangul"] += 1
        elif (
            0x0400 <= codepoint <= 0x052F
            or 0x2DE0 <= codepoint <= 0x2DFF
            or 0xA640 <= codepoint <= 0xA69F
        ):
            counts["cyr"] += 1
        elif (
            0x0600 <= codepoint <= 0x06FF
            or 0x0750 <= codepoint <= 0x077F
            or 0x08A0 <= codepoint <= 0x08FF
        ):
            counts["arabic"] += 1
        elif 0x0E00 <= codepoint <= 0x0E7F:
            counts["thai"] += 1
        else:
            counts["other"] += 1

        if seen >= limit:
            break

    total = sum(counts.values())
    if total == 0:
        return counts, "ascii"

    top_counts = sorted(counts.values(), reverse=True)
    if top_counts[1] >= max(8, top_counts[0] * 0.35):
        return counts, "mixed"

    return counts, max(counts, key=counts.get)


@lru_cache(maxsize=4096)
def _estimate_text_tokens(text: str) -> int:
    """Fast token estimate using C-backed string primitives."""
    if not text:
        return 0

    char_count = len(text)
    spaces = text.count(" ") + text.count("\t")
    newlines = text.count("\n") + text.count("\r")

    if text.isascii():
        punctuation = sum(text.count(ch) for ch in ASCII_PUNCTUATION_CHARS)
        codeish = newlines > 0 or punctuation * 6 > char_count
        if codeish:
            estimate = (
                char_count * 0.20 + spaces * 0.10 + newlines * 0.18 + punctuation * 0.08
            )
        else:
            estimate = char_count * 0.17 + spaces * 0.24 + punctuation * 0.05

        return max(1, math.ceil(estimate))

    ascii_chars = len(text.encode("ascii", "ignore"))
    non_ascii_bytes = len(text.encode("utf-8")) - ascii_chars
    script_counts, script_profile = _sample_script_mix(text)
    sampled_total = max(1, sum(script_counts.values()))
    byte_coefficient = sum(
        (script_counts[key] / sampled_total) * SCRIPT_BYTE_COEFFICIENTS[key]
        for key in SCRIPT_BYTE_COEFFICIENTS
    )
    estimate = (
        ascii_chars * 0.21
        + non_ascii_bytes * byte_coefficient
        + (spaces + newlines) * 0.08
    )

    if script_profile in ("cyr", "arabic"):
        estimate += newlines * 0.04
    else:
        estimate += newlines * 0.08

    if char_count < 256 and script_profile not in ("cyr", "arabic"):
        estimate *= 0.94

    return max(1, math.ceil(estimate))


@lru_cache(maxsize=1024)
def _get_cached_tokens(text: str) -> int:
    """Calculates tokens with LRU caching for exact string matches."""
    if not text:
        return 0
    if TIKTOKEN_ENCODING:
        try:
            # tiktoken logic is relatively fast, but caching it based on exact string match
            # turns O(N) encoding time to O(1) dictionary lookup for historical messages.
            return len(TIKTOKEN_ENCODING.encode(text))
        except Exception as e:
            logger.warning(
                f"[Token Count] tiktoken error: {e}, falling back to character estimation"
            )
            pass

    return _estimate_text_tokens(text)

class TokenMixin:

    """Token counting (tiktoken + fast estimator)."""

    def _count_tokens(self, text: str) -> int:
        """Counts the number of tokens in the text."""
        return _get_cached_tokens(text)

    def _extract_text_content(self, content: Any) -> str:
        """Extract human-readable text from string, multimodal list, or dict payloads."""
        if isinstance(content, str):
            return content

        if isinstance(content, dict):
            text_value = content.get("text")
            if isinstance(text_value, str):
                return text_value
            nested_content = content.get("content")
            if isinstance(nested_content, str):
                return nested_content
            return ""

        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    text_value = part.get("text")
                    if isinstance(text_value, str) and text_value:
                        text_parts.append(text_value)
                        continue

                    nested_content = part.get("content")
                    if isinstance(nested_content, str) and nested_content:
                        text_parts.append(nested_content)

            return " ".join(text_parts)

        return str(content) if content is not None else ""

    def _message_content_char_length(self, content: Any) -> int:
        return len(self._extract_text_content(content))

    def _estimate_content_tokens(self, content: Any) -> int:
        return _estimate_text_tokens(self._extract_text_content(content))

    def _calculate_messages_tokens(self, messages: List[Dict]) -> int:
        """Calculates the total tokens for a list of messages."""
        start_time = time.time()
        total_tokens = 0
        for msg in messages:
            content = self._extract_text_content(msg.get("content", ""))
            total_tokens += self._count_tokens(content)

        duration = (time.time() - start_time) * 1000
        if self.valves.debug_mode:
            logger.info(
                f"[Token Calc] Calculated {total_tokens} tokens for {len(messages)} messages in {duration:.2f}ms"
            )

        return total_tokens

    def _estimate_messages_tokens(self, messages: List[Dict]) -> int:
        """Fast estimation of tokens using mixed-script heuristics."""
        total_tokens = 0
        for msg in messages:
            total_tokens += self._estimate_content_tokens(msg.get("content", ""))

        return total_tokens
