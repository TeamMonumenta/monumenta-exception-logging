# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import hashlib
import re
from typing import Any

_UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE,
)
_BARE_UUID_RE = re.compile(r'\b[0-9a-f]{32}\b', re.IGNORECASE)
_IP_RE = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
_LONG_TOKEN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{31,}')
_NUM_RE = re.compile(r'\d+(?:\.\d+)*')
_WORLD_DISTANCE_RE = re.compile(
    r'(measure distance between )[-_a-z0-9<>]+( and )[-_a-z0-9<>]+',
    re.IGNORECASE,
)
_QUOTED_SINGLE_RE = re.compile(r"(?<!\w)'[^']{1,64}'")
_QUOTED_DOUBLE_RE = re.compile(r'"[^"]{1,64}"')
_BRACKET_DATA_RE = re.compile(r'\[[^\[\]]{0,256}\]')
# Handles one level of nested braces (e.g. Location{world=CraftWorld{name=quests},...}).
_LOCATION_BLOCK_RE = re.compile(r'Location\{world\=\w+\{[^\{\}]+\}[^\{\}]+\}')


def normalize_message(message: str) -> str:
    s = _UUID_RE.sub('<uuid>', message)
    s = _BARE_UUID_RE.sub('<uuid>', s)
    s = _IP_RE.sub('<ip>', s)
    # Long opaque tokens before number replacement so digit-heavy IDs aren't fragmented.
    s = _LONG_TOKEN_RE.sub('<id>', s)
    s = _NUM_RE.sub('<N>', s)
    # World distance after number replacement so numeric world names (plot<N>) still match.
    s = _WORLD_DISTANCE_RE.sub(r'\1<world1>\2<world2>', s)
    s = _QUOTED_SINGLE_RE.sub('<str>', s)
    s = _QUOTED_DOUBLE_RE.sub('<str>', s)
    s = _BRACKET_DATA_RE.sub('<data>', s)
    s = _LOCATION_BLOCK_RE.sub('Location{<location>}', s)
    return s


def extract_app_frames(
    frames: list[dict[str, Any]], app_packages: list[str], count: int
) -> list[dict[str, Any]]:
    result = [
        f for f in frames
        if any(f.get('class_name', '').startswith(pkg) for pkg in app_packages)
    ][:count]
    if not result:
        result = frames[:count]
    return result


def compute_fingerprint(
    exception_class: str, normalized_message: str, top_frames: list[dict[str, Any]]
) -> str:
    frame_str = '|'.join(
        f"{f['class_name']}.{f['method']}" for f in top_frames
    )
    components = [exception_class, normalized_message, frame_str]
    return hashlib.sha256('|'.join(components).encode()).hexdigest()
