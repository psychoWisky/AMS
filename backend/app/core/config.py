from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str = "change-me"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    ALLOWED_ORIGINS: str = "http://localhost:3001"
    AMS_FRONTEND_URL: str = "https://ams.avfu.ac.in"
    UPLOAD_DIR: str = "./uploads"
    MAX_FILE_SIZE_MB: int = 10
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # DEMO ASSUMPTION (Section 28.12, STUDENT_SIDE_IMPLEMENTATION_PLAN.md): the real
    # AVFU university email format is not yet confirmed. This is a configurable
    # placeholder so the real format can be swapped in later without code changes.
    ORIENTATION_EMAIL_DOMAIN: str = "ams.avfu.demo"

    # Email outbox worker (Bulk Upload SMTP timeout fix) — the standalone
    # `python -m app.core.email_worker` process, never the FastAPI app
    # itself. Defaults are conservative for a low-volume (tens-per-batch)
    # AVFU AMS credential-email workload, not tuned for production scale.
    EMAIL_WORKER_POLL_INTERVAL: int = 15       # seconds between polling cycles when idle
    EMAIL_WORKER_MAX_ATTEMPTS: int = 5         # attempts before a job is marked FAILED
    EMAIL_WORKER_BATCH_SIZE: int = 10          # jobs claimed per polling cycle
    EMAIL_WORKER_PROCESSING_TIMEOUT: int = 300  # seconds — PROCESSING lease before a job is recovered as stale

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    model_config = {"env_file": ".env"}

settings = Settings()
