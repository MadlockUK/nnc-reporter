"""Server settings, all from environment variables.

Nothing secret is ever read from a file in the repository. Set these in the
host's secret store (Docker env file, Fly/Render secrets, GitHub Actions
secrets for a deploy job). `python -m nncr.web.manage check` prints what is set.
"""
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .. import config


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    base_url: str                     # https://reports.example.org - used in emails
    secret_key: str
    cookie_secure: bool
    session_idle_minutes: int
    session_max_hours: int
    mfa_required: str                 # "admins" (default), "all" or "none"
    password_min_length: int
    hibp_check: bool
    login_max_failures: int
    lockout_minutes: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_security: str                # starttls | ssl | none
    mapbox_secret_token: str
    mapbox_public_token: str
    mapbox_username: str
    anthropic_key: str
    what3words_key: str
    classify_model: str
    nominatim_email: str
    submit_mode: str
    max_upload_mb: int
    bootstrap_admin: str
    forwarded_allow_ips: str
    host: str
    port: int
    extras: dict = field(default_factory=dict)

    @property
    def mail_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def origin(self) -> str:
        if not self.base_url:
            return ""
        u = urlparse(self.base_url)
        return f"{u.scheme}://{u.netloc}"

    def users_dir(self) -> Path:
        d = self.data_dir / "users"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _secret_key(data_dir: Path) -> str:
    """NNC_SECRET_KEY, or one generated once and kept beside the data (0600).

    It encrypts the council passwords people store, so losing it means they
    re-enter them - set NNC_SECRET_KEY in production so it lives with the
    other secrets rather than on the data volume.
    """
    key = (os.environ.get("NNC_SECRET_KEY") or "").strip()
    if key:
        if len(key) < 32:
            raise SystemExit("NNC_SECRET_KEY must be at least 32 characters")
        return key
    path = data_dir / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def load() -> Settings:
    data_dir = config.shared_dir()
    base_url = (os.environ.get("NNC_BASE_URL") or "").strip().rstrip("/")
    mfa = (os.environ.get("NNC_MFA_REQUIRED") or "admins").strip().lower()
    if mfa not in ("admins", "all", "none"):
        mfa = "admins"
    return Settings(
        data_dir=data_dir,
        base_url=base_url,
        secret_key=_secret_key(data_dir),
        cookie_secure=_flag("NNC_COOKIE_SECURE", base_url.startswith("https://")),
        session_idle_minutes=_int("NNC_SESSION_IDLE_MINUTES", 120),
        session_max_hours=_int("NNC_SESSION_MAX_HOURS", 12),
        mfa_required=mfa,
        password_min_length=max(12, _int("NNC_PASSWORD_MIN_LENGTH", 12)),
        hibp_check=_flag("NNC_HIBP_CHECK", True),
        login_max_failures=max(3, _int("NNC_LOGIN_MAX_FAILURES", 5)),
        lockout_minutes=max(5, _int("NNC_LOCKOUT_MINUTES", 15)),
        smtp_host=(os.environ.get("SMTP_HOST") or "").strip(),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=(os.environ.get("SMTP_USER") or "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD") or "",
        smtp_from=(os.environ.get("SMTP_FROM") or "").strip(),
        smtp_security=(os.environ.get("SMTP_SECURITY") or "starttls").strip().lower(),
        mapbox_secret_token=(os.environ.get("MAPBOX_SECRET_TOKEN") or "").strip(),
        mapbox_public_token=(os.environ.get("MAPBOX_PUBLIC_TOKEN") or "").strip(),
        mapbox_username=(os.environ.get("MAPBOX_USERNAME") or "").strip(),
        anthropic_key=(os.environ.get("ANTHROPIC_API_KEY") or "").strip(),
        what3words_key=(os.environ.get("WHAT3WORDS_API_KEY") or "").strip(),
        classify_model=(os.environ.get("NNC_CLASSIFY_MODEL") or "").strip(),
        nominatim_email=(os.environ.get("NNC_NOMINATIM_EMAIL") or "").strip(),
        submit_mode=(os.environ.get("NNC_SUBMIT_MODE") or "server").strip().lower(),
        max_upload_mb=max(5, _int("NNC_MAX_UPLOAD_MB", 40)),
        bootstrap_admin=(os.environ.get("NNC_BOOTSTRAP_ADMIN") or "").strip(),
        forwarded_allow_ips=(os.environ.get("NNC_FORWARDED_ALLOW_IPS") or "127.0.0.1").strip(),
        host=(os.environ.get("NNC_HOST") or "127.0.0.1").strip(),
        port=_int("PORT", _int("NNC_PORT", 8712)),
    )


_settings: Settings | None = None


def get() -> Settings:
    global _settings
    if _settings is None:
        _settings = load()
        # The submission engine reads its mode from the environment.
        os.environ.setdefault("NNC_SUBMIT_MODE", _settings.submit_mode)
    return _settings


def reset() -> None:
    """Forget the cached settings (tests change the environment)."""
    global _settings
    _settings = None
