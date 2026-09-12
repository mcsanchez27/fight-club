"""Allowlisted wiki retrieval via stdlib urllib (no extra deps)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from bot.sources import (
    cache_ttl_seconds,
    cap_snippet,
    detect_franchise,
    franchise_sources,
    is_hard_no_url,
    load_sources_config,
    url_allowed,
)

USER_AGENT = "FightClubCourt/0.3 (+https://github.com/mcsanchez27/fight-club; receipts)"
FETCH_TIMEOUT = 8
