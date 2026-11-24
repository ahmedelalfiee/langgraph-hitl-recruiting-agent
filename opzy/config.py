"""Settings from .env and the business profile from business.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    # "openai" or "anthropic"; if empty, use whichever key is filled in
    llm_provider: str = os.getenv("LLM_PROVIDER", "") or ("openai" if os.getenv("OPENAI_API_KEY") else "anthropic")
    # Which emails Opzy may read. Keep it narrow when using a personal inbox.
    gmail_query: str = os.getenv("GMAIL_QUERY", "in:inbox newer_than:2d -from:me")
    google_credentials_file: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    google_token_file: str = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    calendar_id: str = os.getenv("CALENDAR_ID", "primary")
    twilio_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from: str = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "30"))
    db_path: str = os.getenv("DB_PATH", str(ROOT / "opzy.db"))


@dataclass
class Business:
    raw: dict = field(default_factory=dict)

    def __getattr__(self, k):
        try:
            return self.raw[k]
        except KeyError as e:
            raise AttributeError(k) from e

    def roles_text(self) -> str:
        return "\n".join(f"- {r['title']}: {r.get('notes', '')}" for r in self.raw["open_roles"])


def load_business(path: str | Path | None = None) -> Business:
    p = Path(path) if path else ROOT / "business.yaml"
    with open(p, encoding="utf-8") as f:
        return Business(yaml.safe_load(f))


settings = Settings()
