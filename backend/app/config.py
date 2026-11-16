from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App & Security
    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    allowed_emails: str = ""

    # Telegram & 3 Dedicated Channels
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    allowed_telegram_user_ids: str = ""
    telegram_production_chat_id: str = ""
    telegram_munim_chat_id: str = ""
    telegram_attendance_chat_id: str = ""
    # Legacy aliases (auto-mapped when new channel vars are unset)
    telegram_floor_chat_id: str = ""
    telegram_accounts_chat_id: str = ""
    telegram_morning_brief_chat_id: str = ""
    admin_telegram_user_id: str = ""
    telegram_burst_debounce_seconds: float = 2.5

    # Supabase (PostgreSQL + Auth)
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    database_url: str = ""

    # Gemini API (Decisions API + Hydrogen Interactions API + Lite Batch)
    gemini_api_key: str = ""
    gemini_decision_model: str = "gemini-decision-1"
    gemini_live_model: str = "gemini-4-hydrogen"
    gemini_lite_model: str = "gemini-3.5-flash-lite"

    # Zoho Books India (Standard Plan ~₹749/mo, GST-Off Management Ledger)
    zoho_books_api_base: str = "https://www.zohoapis.in/books/v3"
    zoho_accounts_base: str = "https://accounts.zoho.in"
    zoho_org_id: str = ""
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_refresh_token: str = ""
    zoho_project_tag_id: str = ""
    zoho_project_custom_field: str = "cf_project_code"

    # GCP Storage, Cloud Tasks & Backup Sheet
    gcp_project_id: str = ""
    gcp_region: str = "asia-south1"
    gcs_storage_bucket: str = "workshop-munim-assets"
    cloud_tasks_queue: str = "telegram-worker-queue"
    cron_auth_secret: str = ""
    backup_sheet_id: str = ""

    @property
    def effective_production_chat_id(self) -> str:
        return (self.telegram_production_chat_id or self.telegram_floor_chat_id).strip()

    @property
    def effective_munim_chat_id(self) -> str:
        return (self.telegram_munim_chat_id or self.telegram_accounts_chat_id).strip()

    @property
    def allowed_email_set(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.allowed_emails.split(",")
            if e.strip()
        }

    @property
    def allowed_telegram_id_set(self) -> set[int]:
        ids: set[int] = set()
        for raw in self.allowed_telegram_user_ids.split(","):
            raw = raw.strip()
            if raw.isdigit():
                ids.add(int(raw))
        return ids

    @property
    def zoho_enabled(self) -> bool:
        return bool(
            self.zoho_org_id
            and self.zoho_client_id
            and self.zoho_client_secret
            and self.zoho_refresh_token
        )

    @property
    def cloud_tasks_enabled(self) -> bool:
        return (
            self.app_env == "production"
            and bool(self.gcp_project_id)
            and bool(self.app_base_url)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
