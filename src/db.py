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


def _call_db_sync(method, *args, **kwargs):
    """
    Call an OpenWebUI DB model method with version-aware async handling (for sync contexts).
    - OpenWebUI <  0.9.0: DB methods are sync, call directly.
    - OpenWebUI >= 0.9.0: DB methods are async, run in a separate thread with its own event loop.
    """
    if not _owui_version_ge("0.9.0"):
        return method(*args, **kwargs)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, method(*args, **kwargs)).result()


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

    async def _load_summary(self, chat_id: str, body: dict) -> Optional[str]:
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
