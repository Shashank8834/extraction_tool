from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://extraction:extraction@localhost:5432/extraction"
    secret_key: str = "dev-insecure-secret-change-me"
    file_encryption_key: str = ""  # Fernet key; if empty, files are stored unencrypted (dev only)
    admin_username: str = "admin"
    admin_password: str = "admin"
    base_url: str = "http://localhost:8000"
    max_upload_mb: int = 15
    link_expiry_days: int = 7  # default lifetime of an intake link (0 = never expires)

    upload_dir: str = "/data/uploads"


settings = Settings()
