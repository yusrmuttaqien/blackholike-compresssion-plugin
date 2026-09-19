"""
title: Async Context Compression
id: async_context_compression
author: Fu-Jie
author_url: https://github.com/Fu-Jie/openwebui-extensions
funding_url: https://github.com/open-webui
description: Reduces token consumption in long conversations while maintaining coherence through intelligent summarization and message compression.
version: 1.6.1
openwebui_id: b1655bc8-6de9-4cad-8cb5-a6f7829a02ce
license: MIT
"""


from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List, Callable, Awaitable
import re
import asyncio
import json
import hashlib
import math
import time
import contextlib
import logging
from inspect import iscoroutinefunction
from copy import deepcopy
from functools import lru_cache

# Setup logger
logger = logging.getLogger(__name__)

SUMMARY_METADATA_SOURCE = "async_context_compression"

# Open WebUI built-in imports
from open_webui.utils.chat import generate_chat_completion
from open_webui.models.users import Users
from open_webui.models.models import Models
from fastapi.requests import Request
from open_webui.main import app as webui_app

try:
    from open_webui.models.chats import Chats
except ModuleNotFoundError:  # pragma: no cover - filter runs inside OpenWebUI
    Chats = None

# Open WebUI internal database (re-use shared connection)
try:
    from open_webui.internal import db as owui_db
except ModuleNotFoundError:  # pragma: no cover - filter runs inside Open WebUI
    owui_db = None

# Try to import tiktoken
try:
    import tiktoken
except ImportError:
    tiktoken = None

# Database imports
from sqlalchemy import Column, String, Text, DateTime, Integer, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.engine import Engine
from datetime import datetime, timezone

# ── i18n.py · Language resolution + translation lookup ────────────────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


TRANSLATIONS = {
    "en-US": {
        "status_context_usage": "Context Usage (Estimated): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_history_usage": "History Usage (Estimated): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_compaction_drives": " | (drives compaction)",
        "status_high_usage": " | ⚠️ High Usage",
        "status_loaded_summary": "Loaded historical summary (Hidden {count} historical messages)",
        "status_context_summary_updated": "Context Summary Updated: {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_generating_summary": "Generating context summary in background...",
        "status_summary_error": "Summary Error: {error} | Check browser console (F12) for details",
        "status_external_refs_injected": "Bypassed chat RAG and injected {count} referenced chat context(s)",
        "summary_prompt_prefix": "【Previous Summary: The following is a summary of the historical conversation, provided for context only. Do not reply to the summary content itself; answer the subsequent latest questions directly.】\n\n",
        "summary_prompt_suffix": "\n\n---\nBelow is the recent conversation:",
        "tool_trimmed": "... [Tool outputs trimmed]\n{content}",
        "content_collapsed": "\n... [Content collapsed] ...\n",
    },
    "zh-CN": {
        "status_context_usage": "上下文用量 (预估): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_history_usage": "对话历史用量 (预估): {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_compaction_drives": " | (触发压缩)",
        "status_high_usage": " | ⚠️ 用量较高",
        "status_loaded_summary": "已加载历史总结 (隐藏了 {count} 条历史消息)",
        "status_context_summary_updated": "上下文总结已更新: {tokens} / {max_tokens} Tokens ({ratio}%)",
        "status_generating_summary": "正在后台生成上下文总结...",
        "status_summary_error": "总结生成错误: {error} | 请查看浏览器控制台(F12)获取详情",
        "status_external_refs_injected": "已绕过 chat RAG，并注入 {count} 个引用聊天上下文",
        "summary_prompt_prefix": "【前情提要：以下是历史对话的总结，仅供上下文参考。请不要回复总结内容本身，直接回答之后最新的问题。】\n\n",
        "summary_prompt_suffix": "\n\n---\n以下是最近的对话：",
        "tool_trimmed": "... [工具输出已裁剪]\n{content}",
        "content_collapsed": "\n... [内容已折叠] ...\n",
    },
}

class I18nMixin:

    """Language resolution + translation lookup."""

    def _resolve_language(self, lang: str) -> str:
        """Resolve the best matching language code from the TRANSLATIONS dict."""
        target_lang = lang

        # 1. Direct match
        if target_lang in TRANSLATIONS:
            return target_lang

        # 2. Variant fallback (explicit mapping)
        if target_lang in self.fallback_map:
            target_lang = self.fallback_map[target_lang]
            if target_lang in TRANSLATIONS:
                return target_lang

        # 3. Base language fallback (e.g. fr-BE -> fr-FR)
        if "-" in lang:
            base_lang = lang.split("-")[0]
            for supported_lang in TRANSLATIONS:
                if supported_lang.startswith(base_lang + "-"):
                    return supported_lang

        # 4. Final Fallback to en-US
        return "en-US"

    def _get_translation(self, lang: str, key: str, **kwargs) -> str:
        """Get translated string for the given language and key."""
        target_lang = self._resolve_language(lang)
        lang_dict = TRANSLATIONS.get(target_lang, TRANSLATIONS["en-US"])
        text = lang_dict.get(key, TRANSLATIONS["en-US"].get(key, key))
        if kwargs:
            try:
                text = text.format(**kwargs)
            except Exception as e:
                logger.warning(f"Translation formatting failed for {key}: {e}")
        return text

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

    async def _resolve_context_tokens(
        self, messages: List[Dict], limit_tokens: int
    ) -> tuple[int, int, bool]:
        """
        Count message tokens with a fast path: use the heuristic estimate when
        it is clearly below `limit_tokens` (within a 15% margin); otherwise
        fall back to a precise tiktoken count in a worker thread.
        Returns (total_tokens, estimated_tokens, used_precise_count).
        """
        estimated_tokens = self._estimate_messages_tokens(messages)
        if limit_tokens > 0 and estimated_tokens < limit_tokens * 0.85:
            return estimated_tokens, estimated_tokens, False
        precise_tokens = await asyncio.to_thread(
            self._calculate_messages_tokens, messages
        )
        return precise_tokens, estimated_tokens, True

# ── db.py · DB engine/schema discovery, ChatSummary model, sessions, summary persistence 
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


def _discover_owui_engine(db_module: Any) -> Optional[Engine]:
    """Discover the Open WebUI SQLAlchemy engine via provided db module helpers."""
    if db_module is None:
        return None

    db_context = getattr(db_module, "get_db_context", None) or getattr(
        db_module, "get_db", None
    )
    if callable(db_context):
        try:
            with db_context() as session:
                try:
                    return session.get_bind()
                except AttributeError:
                    return getattr(session, "bind", None) or getattr(
                        session, "engine", None
                    )
        except Exception as exc:
            logger.error(f"[DB Discover] get_db_context failed: {exc}")

    for attr in ("engine", "ENGINE", "bind", "BIND"):
        candidate = getattr(db_module, attr, None)
        if candidate is not None:
            return candidate

    return None


def _discover_owui_schema(db_module: Any) -> Optional[str]:
    """Discover the Open WebUI database schema name if configured."""
    if db_module is None:
        return None

    try:
        base = getattr(db_module, "Base", None)
        metadata = getattr(base, "metadata", None) if base is not None else None
        candidate = getattr(metadata, "schema", None) if metadata is not None else None
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    except Exception as exc:
        logger.error(f"[DB Discover] Base metadata schema lookup failed: {exc}")

    try:
        metadata_obj = getattr(db_module, "metadata_obj", None)
        candidate = (
            getattr(metadata_obj, "schema", None) if metadata_obj is not None else None
        )
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    except Exception as exc:
        logger.error(f"[DB Discover] metadata_obj schema lookup failed: {exc}")

    try:
        from open_webui import env as owui_env

        candidate = getattr(owui_env, "DATABASE_SCHEMA", None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    except Exception as exc:
        logger.error(f"[DB Discover] env schema lookup failed: {exc}")

    return None


owui_engine = _discover_owui_engine(owui_db)
owui_schema = _discover_owui_schema(owui_db)
owui_Base = getattr(owui_db, "Base", None) if owui_db is not None else None
if owui_Base is None:
    owui_Base = declarative_base()

# ── OpenWebUI version detection for async DB compatibility ──────────
try:
    from open_webui.env import VERSION as _owui_version
except ImportError:
    _owui_version = "0.0.0"


def _owui_version_ge(threshold: str) -> bool:
    """Return True if open_webui_version >= threshold (e.g. '0.9.0')."""
    try:
        v = [int(x) for x in _owui_version.split(".")[:3]]
        t = [int(x) for x in threshold.split(".")[:3]]
        return v >= t
    except (ValueError, TypeError):
        return False


async def _call_db(method, *args, **kwargs):
    """
    Call an OpenWebUI DB model method with version-aware async handling.
    - OpenWebUI >= 0.9.0: DB methods are async, so we await them.
    - OpenWebUI <  0.9.0: DB methods are sync, so we call them directly.
    """
    if _owui_version_ge("0.9.0"):
        return await method(*args, **kwargs)
    else:
        return method(*args, **kwargs)


_db_sync_pool = None


def _get_db_sync_pool():
    """Shared single-worker pool for bridging async DB calls into sync contexts."""
    global _db_sync_pool
    if _db_sync_pool is None:
        import concurrent.futures

        _db_sync_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="owui-db"
        )
    return _db_sync_pool


def _call_db_sync(method, *args, **kwargs):
    """
    Call an OpenWebUI DB model method with version-aware async handling (for sync contexts).
    - OpenWebUI <  0.9.0: DB methods are sync, call directly.
    - OpenWebUI >= 0.9.0: DB methods are async, run on a shared worker thread with its own event loop.
    """
    if not _owui_version_ge("0.9.0"):
        return method(*args, **kwargs)
    return _get_db_sync_pool().submit(asyncio.run, method(*args, **kwargs)).result()


class ChatSummary(owui_Base):
    """Chat Summary Storage Table"""

    __tablename__ = "chat_summary"
    __table_args__ = (
        {"extend_existing": True, "schema": owui_schema}
        if owui_schema
        else {"extend_existing": True}
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String(255), unique=True, nullable=False, index=True)
    summary = Column(Text, nullable=False)
    compressed_message_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

class DBMixin:

    """DB engine/schema discovery, ChatSummary model, sessions, summary persistence."""

    @contextlib.asynccontextmanager
    async def _async_db_session(self):
        """
        Yield an async-capable database session.

        - OpenWebUI >= 0.9.0: uses ``get_async_db_context`` (AsyncSession).
        - OpenWebUI <  0.9.0: wraps the sync ``_db_session`` via ``asyncio.to_thread``.
        """
        db_module = self._owui_db
        async_ctx = getattr(db_module, "get_async_db_context", None)
        if callable(async_ctx):
            async with async_ctx() as session:
                yield session
                return
        async_db = getattr(db_module, "get_async_db", None)
        if callable(async_db):
            async with async_db() as session:
                yield session
                return

        # Fallback: wrap sync session in a thread for < 0.9.0
        # (sync pool is safe to use in < 0.9.0 since there's no competing async pool)
        with self._sync_db_session() as session:
            yield session

    @contextlib.contextmanager
    def _sync_db_session(self):
        """Yield a SYNC database session — reserved for startup/initialization only."""
        db_module = self._owui_db
        db_context = None
        if db_module is not None:
            db_context = getattr(db_module, "get_db_context", None) or getattr(
                db_module, "get_db", None
            )

        if callable(db_context):
            with db_context() as session:
                yield session
                return

        factory = None
        if db_module is not None:
            factory = getattr(db_module, "SessionLocal", None) or getattr(
                db_module, "ScopedSession", None
            )
        if callable(factory):
            session = factory()
            try:
                yield session
            finally:
                close = getattr(session, "close", None)
                if callable(close):
                    close()
            return

        if self._fallback_session_factory is None:
            raise RuntimeError(
                "Open WebUI database session is unavailable. Ensure Open WebUI's database layer is initialized."
            )

        session = self._fallback_session_factory()
        try:
            yield session
        finally:
            try:
                session.close()
            except Exception as exc:  # pragma: no cover - best-effort cleanup
                logger.warning(f"[Database] ⚠️ Failed to close fallback session: {exc}")

    def _init_database(self):
        """Initializes the database table using Open WebUI's shared connection."""
        try:
            if self._db_engine is None:
                raise RuntimeError(
                    "Open WebUI database engine is unavailable. Ensure Open WebUI is configured with a valid DATABASE_URL."
                )

            # Check if table exists using SQLAlchemy inspect
            inspector = inspect(self._db_engine)
            # Support schema if configured
            has_table = (
                inspector.has_table("chat_summary", schema=owui_schema)
                if owui_schema
                else inspector.has_table("chat_summary")
            )

            if not has_table:
                # Create the chat_summary table if it doesn't exist
                ChatSummary.__table__.create(bind=self._db_engine, checkfirst=True)
                logger.info(
                    "[Database] ✅ Successfully created chat_summary table using Open WebUI's shared database connection."
                )
            else:
                logger.info(
                    "[Database] ✅ Using Open WebUI's shared database connection. chat_summary table already exists."
                )

        except Exception as e:
            logger.error(f"[Database] ❌ Initialization failed: {str(e)}")

    async def _save_summary(self, chat_id: str, summary: str, compressed_count: int):
        """Saves the summary to the database (async, compatible with 0.9.0 async sessions)."""
        try:
            async with self._async_db_session() as session:
                # Detect session type: async sessions expose execute as a coroutinefunction
                if iscoroutinefunction(getattr(session, "execute", None)):
                    # SQLAlchemy 2.0 async style (AsyncSession)
                    from sqlalchemy import select

                    result = await session.execute(
                        select(ChatSummary).filter_by(chat_id=chat_id)
                    )
                    existing = result.scalars().first()

                    if existing:
                        # Optimistic lock: skip if progress hasn't advanced
                        if compressed_count <= existing.compressed_message_count:
                            if self.valves.debug_mode:
                                logger.info(
                                    f"[Storage] Skipping update: New progress ({compressed_count}) "
                                    f"<= existing ({existing.compressed_message_count})"
                                )
                            return
                        existing.summary = summary
                        existing.compressed_message_count = compressed_count
                        existing.updated_at = datetime.now(timezone.utc)
                    else:
                        new_summary = ChatSummary(
                            chat_id=chat_id,
                            summary=summary,
                            compressed_message_count=compressed_count,
                        )
                        session.add(new_summary)

                    await session.commit()

                    if self.valves.debug_mode:
                        action = "Updated" if existing else "Created"
                        logger.info(
                            f"[Storage] Summary has been {action.lower()} in the database (Chat ID: {chat_id})"
                        )
                else:
                    # < 0.9.0: sync session (Session)
                    existing = (
                        session.query(ChatSummary).filter_by(chat_id=chat_id).first()
                    )

                    if existing:
                        if compressed_count <= existing.compressed_message_count:
                            if self.valves.debug_mode:
                                logger.info(
                                    f"[Storage] Skipping update: New progress ({compressed_count}) "
                                    f"<= existing ({existing.compressed_message_count})"
                                )
                            return
                        existing.summary = summary
                        existing.compressed_message_count = compressed_count
                        existing.updated_at = datetime.now(timezone.utc)
                    else:
                        new_summary = ChatSummary(
                            chat_id=chat_id,
                            summary=summary,
                            compressed_message_count=compressed_count,
                        )
                        session.add(new_summary)

                    session.commit()

                    if self.valves.debug_mode:
                        action = "Updated" if existing else "Created"
                        logger.info(
                            f"[Storage] Summary has been {action.lower()} in the database (Chat ID: {chat_id})"
                        )

        except Exception as e:
            logger.error(f"[Storage] ❌ Database save failed: {str(e)}")

    async def _load_summary_record(self, chat_id: str) -> Optional[ChatSummary]:
        """Loads the summary record object from the database (async, compatible with 0.9.0)."""
        try:
            async with self._async_db_session() as session:
                # Detect session type: async sessions expose execute as a coroutinefunction
                if iscoroutinefunction(getattr(session, "execute", None)):
                    # SQLAlchemy 2.0 async style (AsyncSession)
                    from sqlalchemy import select

                    result = await session.execute(
                        select(ChatSummary).filter_by(chat_id=chat_id)
                    )
                    record = result.scalars().first()
                    if record:
                        return record
                else:
                    # < 0.9.0: sync session (Session)
                    record = (
                        session.query(ChatSummary).filter_by(chat_id=chat_id).first()
                    )
                    if record:
                        session.expunge(record)
                        return record
        except Exception as e:
            logger.error(f"[Load] ❌ Database read failed: {str(e)}")
        return None

    async def _load_summary(self, chat_id: str) -> Optional[str]:
        """Loads the summary text from the database (async, compatible with 0.9.0)."""
        record = await self._load_summary_record(chat_id)
        if record:
            if self.valves.debug_mode:
                logger.info(f"[Load] Loaded summary from database (Chat ID: {chat_id})")
                logger.info(
                    f"[Load] Last updated: {record.updated_at}, Compressed message count: {record.compressed_message_count}"
                )
            return record.summary
        return None

# ── contextlength.py · Model-reported context length resolution ───────
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.
#
# A model's max context is taken from the model API wherever possible
# instead of the global valve. Open WebUI stores the model object the
# API returned in the model's `meta`, so for OpenAI-compatible APIs
# (OpenAI, llama.cpp server, vLLM, LM Studio, OpenRouter, ...) no live
# query is needed — `meta.context_length` is authoritative.
#
# To support a new API type:
#   1. Write a resolver: (model_id, model_obj) -> Optional[int]
#   2. Register it in _CONTEXT_LENGTH_RESOLVERS under the API type name.


def _resolve_context_length_openai_api(model_id, model_obj) -> Optional[int]:
    """OpenAI-compatible: Open WebUI stores the /v1/models payload in meta.

    Field precedence: `context_length` (OpenAI standard) then `n_ctx`
    (llama.cpp — the slot context size the server enforces; `n_ctx_train`
    is the trained size and is deliberately ignored). Both are checked at
    the meta top level and one level down under 'meta': llama.cpp nests
    its fields in a 'meta' sub-object, and Open WebUI flattens them into
    the model meta on import, so either shape may occur.
    """

    def _pick(source):
        if not isinstance(source, dict):
            return None
        for key in ("context_length", "n_ctx"):
            value = source.get(key)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and value > 0
            ):
                return int(value)
        return None

    meta = getattr(model_obj, "meta", None)
    nested = meta.get("meta") if isinstance(meta, dict) else None
    return _pick(meta) or _pick(nested)


# API type -> resolver. Add entries here when supporting new API types
# (e.g. "ollama": ... once an Ollama /api/show resolver exists).
_CONTEXT_LENGTH_RESOLVERS = {
    "openai_api": _resolve_context_length_openai_api,
}

# API types known to Open WebUI connections, used for model-id prefix
# detection below.
_KNOWN_API_TYPES = frozenset(
    {
        "openai_api",
        "ollama",
        "openrouter",
        "lm_studio",
        "vllm",
        "groq",
        "mistral",
        "anthropic",
        "gemini",
        "bedrock",
    }
)


def _detect_api_type(model_id: str) -> str:
    """Best-effort detection of a model's API type.

    Connection model ids are namespaced as 'connection_name/model_id'
    and default connection names match the API type. When no prefix
    match is found, assume OpenAI-compatible (the default for custom
    connections). A Connections-table lookup can replace this later if
    renamed connections prove to be a problem.
    """
    prefix = model_id.split("/", 1)[0] if "/" in model_id else ""
    if prefix in _KNOWN_API_TYPES:
        return prefix
    return "openai_api"


def _resolve_reported_context_length(model_id: str, model_obj) -> Optional[int]:
    """Query the registered resolver for the model's max context tokens.

    Returns None when the model/API does not report one; callers fall
    back to the global max_context_tokens valve.
    """
    if model_obj is None:
        return None
    api_type = _detect_api_type(model_id)
    resolver = _CONTEXT_LENGTH_RESOLVERS.get(
        api_type, _resolve_context_length_openai_api
    )
    try:
        return resolver(model_id, model_obj)
    except Exception:
        return None

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

    def _trim_native_tool_outputs(self, messages: List[Dict], lang: str) -> int:
        """Collapse verbose native tool outputs while preserving tool-call structure."""
        trimmed_count = 0
        tool_trim_threshold_chars = self.valves.tool_trim_threshold_chars
        collapsed_text = self._get_translation(lang, "content_collapsed").strip()
        atomic_groups = self._get_atomic_groups(messages)

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
            if tool_chars < tool_trim_threshold_chars:
                continue

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

            def _replace_tool_block(match: re.Match) -> str:
                nonlocal trimmed_blocks
                block = match.group(0)
                result_match = re.search(r'result="([^"]*)"', block)

                if not result_match:
                    return block

                result_chars = len(result_match.group(1))
                if result_chars < tool_trim_threshold_chars:
                    return block

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

        return trimmed_count

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

    def _drop_oldest_atomic_group(
        self,
        trimmable: List[Dict],
        group_indices: List[int],
        count_precise: bool,
        preserve_protected: bool,
        preserved_systems: Optional[List[Dict]],
    ) -> int:
        """
        Drop the oldest atomic group from the front of `trimmable`.
        When `preserve_protected` is set, external-reference messages stop the
        drop (re-inserted at the front) and system messages are moved to
        `preserved_systems` instead of being counted.
        Returns the number of tokens dropped.
        """
        dropped_tokens = 0
        for _ in range(len(group_indices)):
            dropped = trimmable.pop(0)
            if preserve_protected:
                if self._is_external_reference_message(dropped):
                    trimmable.insert(0, dropped)
                    break
                if isinstance(dropped, dict) and dropped.get("role") == "system":
                    preserved_systems.append(dropped)
                    continue
            if count_precise:
                dropped_tokens += self._count_tokens(str(dropped.get("content", "")))
            else:
                dropped_tokens += self._estimate_content_tokens(
                    dropped.get("content", "")
                )
        return dropped_tokens

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

# ── compression.py · History reconstruction, thresholds, compression orchestration 
# Fragment: relies on shared imports/constants from _header.py.
# Not importable standalone — assembled into v1.6.1.py by build.py.


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

    async def _resolve_summary_boundary(
        self,
        messages: list,
        target_compressed_count: Optional[int],
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve the visible message range that maps to the next
        original-history compression boundary.

        Returns a dict with middle_messages, summary_index, base_progress,
        protected_prefix, start_index — or None when there is nothing to
        compress.
        """
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
            return None

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

        return {
            "middle_messages": middle_messages,
            "summary_index": summary_index,
            "base_progress": base_progress,
            "protected_prefix": protected_prefix,
            "start_index": start_index,
        }

    async def _fit_summary_request(
        self,
        body: dict,
        chat_id: str,
        summary_index: Optional[int],
        middle_messages: list,
        protected_prefix: int,
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fit the summary request within the summary model's budget.

        Resolves the summary model and request limits, loads a
        DB-backed previous summary when one is not already in the
        messages, and shrinks the newest atomic groups until the
        fitted prompt fits.

        Returns a dict with middle_messages, previous_summary,
        protected_prefix, prompt_tokens, summary_model_id,
        max_context_tokens, request_limits — or None when there is
        nothing to summarize.
        """
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
            return None

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
            previous_summary = await self._load_summary(chat_id)
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
            return None

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
            return None

        if not middle_messages:
            await self._log(
                "[🤖 Async Summary Task] Middle messages empty after truncation, skipping summary generation",
                event_call=__event_call__,
            )
            return None

        return {
            "middle_messages": middle_messages,
            "previous_summary": previous_summary,
            "protected_prefix": protected_prefix,
            "prompt_tokens": prompt_tokens,
            "summary_model_id": summary_model_id,
            "max_context_tokens": max_context_tokens,
            "request_limits": request_limits,
        }

    async def _emit_post_summary_usage_status(
        self,
        messages: list,
        body: dict,
        new_summary: str,
        saved_compressed_count: int,
        summary_index: Optional[int],
        base_progress: int,
        lang: str,
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> None:
        """
        Emit the post-summary context usage status, assembling the next
        sent context (head + new summary message + tail) from the saved
        original-history boundary.
        """
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
                            sys_content = self._extract_system_from_params(
                                model_obj.params
                            )
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
                token_count = await asyncio.to_thread(
                    self._calculate_messages_tokens, next_context
                )

                # 5. Get Thresholds & Calculate Ratio
                model = self._clean_model_id(body.get("model"))
                thresholds = self._get_model_thresholds(model)
                max_context_tokens = thresholds.get(
                    "max_context_tokens", self.valves.max_context_tokens
                )
                # 6. Emit Status (only if threshold is met)
                await self._emit_context_usage_status(
                    token_count, max_context_tokens, lang, __event_emitter__
                )
            except Exception as e:
                await self._log(
                    f"[Status] Error calculating tokens: {e}",
                    log_type="error",
                    event_call=__event_call__,
                )

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

            # 1. Resolve the visible message range for the next summary
            # boundary (original-history coordinates).
            boundary = await self._resolve_summary_boundary(
                messages, target_compressed_count, __event_call__
            )
            if boundary is None:
                return
            middle_messages = boundary["middle_messages"]
            summary_index = boundary["summary_index"]
            base_progress = boundary["base_progress"]
            protected_prefix = boundary["protected_prefix"]
            start_index = boundary["start_index"]

            # 2. Fit the summary request within the summary model's budget.
            fitted = await self._fit_summary_request(
                body,
                chat_id,
                summary_index,
                middle_messages,
                protected_prefix,
                __event_call__,
            )
            if fitted is None:
                return
            middle_messages = fitted["middle_messages"]
            previous_summary = fitted["previous_summary"]
            protected_prefix = fitted["protected_prefix"]
            prompt_tokens = fitted["prompt_tokens"]
            summary_model_id = fitted["summary_model_id"]
            max_context_tokens = fitted["max_context_tokens"]
            request_limits = fitted["request_limits"]
            # 3. Build conversation text using the fitted request payload.
            conversation_text = self._format_messages_for_summary(middle_messages)
            if max_context_tokens > 0:
                await self._log(
                    f"[🤖 Async Summary Task] Final fitted summary input: {prompt_tokens} / {request_limits['max_input_tokens']} Tokens",
                    event_call=__event_call__,
                )

            # 4. Call LLM to generate new summary

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

            # 5. Save new summary
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

            # 6. Token usage status notification
            await self._emit_post_summary_usage_status(
                messages,
                body,
                new_summary,
                saved_compressed_count,
                summary_index,
                base_progress,
                lang,
                __event_call__,
                __event_emitter__,
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

            logger.exception("[🤖 Async Summary Task] Unhandled exception")

    def _truncate_messages_for_summary(self, messages: list, max_tokens: int) -> str:
        formatted = []
        total_tokens = 0

        for msg in reversed(messages):
            role = msg.get("role", "unknown")
            content = self._extract_text_content(msg.get("content", ""))
            msg_id = msg.get("id", "N/A")
            formatted_msg = f"#### {role.capitalize()} [ID: {msg_id}]\n{content}\n"
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
                try:
                    response = json.loads(response.body.decode("utf-8"))
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
        label_key: str = "status_context_usage",
        note_key: Optional[str] = None,
    ) -> None:
        """Emit a usage status notification if the usage ratio warrants it.

        label_key selects which i18n template to use: 'status_context_usage'
        for full-request counts (inlet, post-summary) and
        'status_history_usage' for history-only counts (outlet background
        check), so the two readings are not mistaken for each other.
        note_key optionally appends a translated suffix (e.g. marking the
        history reading as the one that drives compaction).
        """
        if not __event_emitter__ or max_context_tokens <= 0:
            return
        usage_ratio = tokens / max_context_tokens
        if not self._should_show_status(usage_ratio):
            return
        status_msg = self._get_translation(
            lang,
            label_key,
            tokens=tokens,
            max_tokens=max_context_tokens,
            ratio=f"{usage_ratio * 100:.1f}",
        )
        if usage_ratio > 0.9:
            status_msg += self._get_translation(lang, "status_high_usage")
        if note_key:
            status_msg += self._get_translation(lang, note_key)
        await __event_emitter__(
            {"type": "status", "data": {"description": status_msg, "done": True}}
        )

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
        # Set when frontend broadcasting is unavailable; stops retrying without
        # mutating the user-configurable show_debug_log valve.
        self._frontend_broadcast_broken = False
        self._init_database()
    class Valves(BaseModel):
        priority: int = Field(
            default=10, description="Priority level for the filter operations."
        )
        # Token related parameters
        compression_threshold_percent: float = Field(
            default=80.0,
            ge=0,
            le=100,
            description="Trigger compression when total context reaches this percentage (0-100) of the resolved max context. Resolution order: per-model override → model-reported context length → global max_context_tokens.",
        )
        max_context_tokens: int = Field(
            default=128000,
            ge=0,
            description="Hard limit for context (global fallback). Used only when the model doesn't report its own context length — OpenAI-compatible APIs report context_length, which takes precedence. Exceeding this value forces removal of earliest messages.",
        )
        model_thresholds: str = Field(
            default="",
            description="Per-model overrides (highest priority). Format: model_id:compression_threshold:max_context (comma-separated). Absolute token counts; win over model-reported values and global settings. Example: gpt-4:8000:32000, claude-3:100000:200000",
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
    async def _extract_inlet_system_prompt(
        self,
        body: dict,
        messages: List[Dict],
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Extract the system prompt for accurate token calculation.

        1. For custom models: check DB (Models.get_model_by_id)
        2. For base models: check messages for role='system'

        Returns a system message dict for budget calculation, or None.
        """
        system_prompt_content = None

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
                            system_prompt_content = self._extract_system_from_params(
                                model_obj.params
                            )
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

        return system_prompt_msg

    async def _assemble_summary_view(
        self,
        body: dict,
        messages: List[Dict],
        summary_record,
        lang: str,
        effective_keep_first: int,
        system_prompt_msg: Optional[Dict[str, str]],
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> tuple:
        """
        Build the [Head] + [Summary Message] + [Tail] view for a chat that
        already has a stored summary, fit it to the context budget, and
        emit the sent-context stats and status.

        Returns (final_messages, external_refs_injected_count).
        """
        external_refs_injected_count = 0

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
        # Since this is a hard limit check, only skip precise calculation if we are far below it (margin of 15%)
        # max_context_tokens == 0 means "no limit", skip reduction entirely
        total_tokens, estimated_tokens, used_precise = (
            await self._resolve_context_tokens(calc_messages, max_context_tokens)
        )
        if max_context_tokens <= 0:
            await self._log(
                f"[Inlet] 🔎 No max_context_tokens limit set (0). Skipping reduction. Est: {total_tokens}t",
                event_call=__event_call__,
            )
        elif not used_precise:
            await self._log(
                "[Inlet] 🔎 Sent-context preflight (estimated)\n"
                f"sent_context_tokens={total_tokens} | max_context_tokens={max_context_tokens} | status=well_within_limit",
                event_call=__event_call__,
            )
        else:
            # Preflight Check Log
            await self._log(
                "[Inlet] 🔎 Sent-context preflight (precise)\n"
                f"sent_context_tokens={total_tokens} | max_context_tokens={max_context_tokens} | usage={(total_tokens/max_context_tokens*100):.1f}%",
                event_call=__event_call__,
            )

            # Identify atomic groups to avoid breaking tool-calling context
            atomic_groups = self._get_atomic_groups(tail_messages)

            while total_tokens > max_context_tokens and len(atomic_groups) > 1:
                # Drop the oldest atomic group entirely (never cut through a
                # tool-calling block).
                dropped_group_indices = atomic_groups.pop(0)
                dropped_tokens = self._drop_oldest_atomic_group(
                    tail_messages,
                    dropped_group_indices,
                    used_precise,
                    preserve_protected=False,
                    preserved_systems=None,
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
        if not used_precise:
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
            await self._emit_context_usage_status(
                total_section_tokens, max_context_tokens, lang, __event_emitter__
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


        return final_messages, external_refs_injected_count

    async def _assemble_plain_view(
        self,
        body: dict,
        messages: List[Dict],
        effective_keep_first: int,
        lang: str,
        system_prompt_msg: Optional[Dict[str, str]],
        __event_call__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> tuple:
        """
        Build the sent context for a chat without a stored summary: inject
        external references if present, fit to the context budget, and
        emit the status.

        Returns (final_messages, external_refs_injected_count);
        final_messages is None when there are no messages to send.
        """
        external_refs_injected_count = 0

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
                    return None, external_refs_injected_count

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
        # Only skip precise calculation if we are clearly below the limit
        # max_context_tokens == 0 means "no limit", skip reduction entirely
        total_tokens, estimated_tokens, used_precise = (
            await self._resolve_context_tokens(calc_messages, max_context_tokens)
        )
        if max_context_tokens <= 0:
            await self._log(
                f"[Inlet] 🔎 No max_context_tokens limit set (0). Skipping reduction. Est: {total_tokens}t",
                event_call=__event_call__,
            )
        elif not used_precise:
            await self._log(
                f"[Inlet] 🔎 Fast limit check (Est): {total_tokens}t / {max_context_tokens}t",
                event_call=__event_call__,
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

            # Absolute protections when dropping groups:
            # 1. External references (often large and specialized)
            # 2. System messages (instructions)
            while total_tokens > max_context_tokens and len(atomic_groups) > 1:
                dropped_group_indices = atomic_groups.pop(0)
                dropped_tokens = self._drop_oldest_atomic_group(
                    trimmable,
                    dropped_group_indices,
                    used_precise,
                    preserve_protected=True,
                    preserved_systems=dropped_but_preserved_systems,
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
        await self._emit_context_usage_status(
            total_tokens, max_context_tokens, lang, __event_emitter__
        )

        return final_messages, external_refs_injected_count

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

        if self._should_skip_compression(body):
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
            trimmed_count = self._trim_native_tool_outputs(messages, lang)
            if self.valves.show_debug_log and __event_call__:
                await self._log(
                    f"[Inlet] ✂️ Tool trimming: {trimmed_count} tool output(s) trimmed.",
                    event_call=__event_call__,
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

        # Extract system prompt for accurate token calculation (DB -> messages fallback)
        system_prompt_msg = await self._extract_inlet_system_prompt(
            body, messages, __event_call__
        )

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

        if summary_record:
            # Summary exists: build [Head] + [Summary Message] + [Tail]
            final_messages, external_refs_injected_count = (
                await self._assemble_summary_view(
                    body,
                    messages,
                    summary_record,
                    lang,
                    effective_keep_first,
                    system_prompt_msg,
                    __event_call__,
                    __event_emitter__,
                )
            )
        else:
            # No stored summary: send raw history (with external refs if any)
            final_messages, external_refs_injected_count = (
                await self._assemble_plain_view(
                    body,
                    messages,
                    effective_keep_first,
                    lang,
                    system_prompt_msg,
                    __event_call__,
                    __event_emitter__,
                )
            )
            if final_messages is None:
                # No messages to send — nothing to compress
                return body

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
        if self._should_skip_compression(body):
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
        else:
            summary_messages = self._unfold_messages(messages)

        summary_messages = self._restore_pending_inlet_messages(
            chat_id, summary_messages
        )

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
