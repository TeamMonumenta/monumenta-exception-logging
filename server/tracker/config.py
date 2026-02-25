import os
from dataclasses import dataclass, field


@dataclass
class TrackerConfig:
    db_path: str = "tracker.db"
    app_packages: list[str] = field(default_factory=lambda: ["com.playmonumenta"])
    fingerprint_frame_count: int = 3
    expiry_days: int = 14


def from_env() -> TrackerConfig:
    db_path = os.environ.get("DB_PATH", "tracker.db")
    raw_packages = os.environ.get("APP_PACKAGES", "com.playmonumenta")
    app_packages = [p.strip() for p in raw_packages.split(",") if p.strip()]
    return TrackerConfig(db_path=db_path, app_packages=app_packages)
