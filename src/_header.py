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
from typing import Optional, Dict, Any, List, Union, Callable, Awaitable
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
