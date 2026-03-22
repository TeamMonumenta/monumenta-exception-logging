# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackerConfig:
    db_path: str = "tracker.db"
    app_packages: list[str] = field(default_factory=lambda: ["com.playmonumenta"])
    fingerprint_frame_count: int = 3
    expiry_days: int = 14
    verbose: bool = True
    # Chisel integration — disabled when chisel_public_url is None
    chisel_public_url: Optional[str] = None
    chisel_fix_prompt_path: str = "fix_exception_prompt.md"
    reaction_fix_request: str = "\U0001F527"   # 🔧
    reaction_fix_working: str = "\U0001F504"   # 🔄
    reaction_fix_success: str = "\U0001F7E2"   # 🟢
    reaction_fix_failure: str = "\U0001F534"   # 🔴
    reaction_fix_declined: str = "\U0001F7E1"  # 🟡


def from_env() -> TrackerConfig:
    db_path = os.environ.get("DB_PATH", "tracker.db")
    raw_packages = os.environ.get("APP_PACKAGES", "com.playmonumenta")
    app_packages = [p.strip() for p in raw_packages.split(",") if p.strip()]
    verbose = os.environ.get("VERBOSE", "true").lower() not in ("false", "0", "no")
    expiry_days = int(os.environ.get("EXPIRY_DAYS", "14"))
    chisel_public_url = os.environ.get("CHISEL_PUBLIC_URL") or None
    chisel_fix_prompt_path = os.environ.get(
        "CHISEL_FIX_PROMPT_PATH", "fix_exception_prompt.md"
    )
    return TrackerConfig(
        db_path=db_path,
        app_packages=app_packages,
        verbose=verbose,
        expiry_days=expiry_days,
        chisel_public_url=chisel_public_url,
        chisel_fix_prompt_path=chisel_fix_prompt_path,
        reaction_fix_request=os.environ.get("REACTION_FIX_REQUEST", "\U0001F527"),
        reaction_fix_working=os.environ.get("REACTION_FIX_WORKING", "\U0001F504"),
        reaction_fix_success=os.environ.get("REACTION_FIX_SUCCESS", "\U0001F7E2"),
        reaction_fix_failure=os.environ.get("REACTION_FIX_FAILURE", "\U0001F534"),
        reaction_fix_declined=os.environ.get("REACTION_FIX_DECLINED", "\U0001F7E1"),
    )
