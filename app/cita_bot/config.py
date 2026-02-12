from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, field_validator

load_dotenv()


class Settings(BaseModel):
    tg_bot_token: str = Field(alias="TG_BOT_TOKEN")
    admins: List[int] = Field(default_factory=list, alias="ADMINS")

    target_url: HttpUrl = Field(alias="TARGET_URL")
    default_interval_seconds: int = Field(default=180, alias="DEFAULT_INTERVAL_SECONDS")

    data_dir: Path = Field(default=Path("/opt/cita_bot/data"), alias="DATA_DIR")
    log_dir: Path = Field(default=Path("/opt/cita_bot/logs"), alias="LOG_DIR")
    db_path: Path = Field(default=Path("/opt/cita_bot/data/bot.sqlite3"), alias="DB_PATH")

    screenshot_on_slots: bool = Field(default=True, alias="SCREENSHOT_ON_SLOTS")

    @field_validator("admins", mode="before")
    @classmethod
    def parse_admins(cls, v):
        # allow "1,2,3" or ["1","2"]
        if v is None or v == "":
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return [int(p) for p in parts]
        if isinstance(v, (list, tuple)):
            return [int(x) for x in v]
        return v

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "screenshots").mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    missing = []
    for key in ["TG_BOT_TOKEN", "TARGET_URL"]:
        if not os.getenv(key):
            missing.append(key)
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}. Create /opt/cita_bot/app/.env")
    s = Settings.model_validate(os.environ)
    s.ensure_dirs()
    return s
