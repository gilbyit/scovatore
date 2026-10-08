"""Configurazione da variabili d'ambiente (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv e' opzionale: in Docker le variabili arrivano gia' dall'env_file
    load_dotenv = None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "si", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else default


def _str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    return raw.strip() if raw is not None else default


@dataclass
class LLMConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    tpm_limit: int          # token al minuto, 0 = nessun limite
    json_mode: bool
    reasoning_effort: str   # vuoto = non inviato
    temperature: float
    timeout: float
    max_tokens: int


@dataclass
class Config:
    # eBay
    ebay_app_id: str
    ebay_cert_id: str
    ebay_env: str                 # production | sandbox
    ebay_buyer_country: str       # per X-EBAY-C-ENDUSERCTX (costi di spedizione corretti)
    ebay_buyer_zip: str
    ebay_daily_call_budget: int

    # LLM
    palantir: LLMConfig
    groq: LLMConfig

    # Pipeline
    screen_batch_size: int
    description_max_chars: int
    max_verify_per_run: int
    notify_min_score: int

    # Percorsi
    data_dir: Path
    hunts_dir: Path
    db_path: Path

    # Notifiche
    ntfy_url: str
    ntfy_token: str

    log_level: str

    # Interfaccia web
    web_host: str = "0.0.0.0"
    web_port: int = 8482
    web_token: str = ""           # vuoto = nessuna protezione (uso in LAN)

    # Chi genera il piano delle query: palantir | groq
    plan_llm: str = "palantir"

    # Dominio dei link agli annunci (interfaccia e notifiche): lo stesso ID funziona su
    # tutti i siti eBay, e su quello italiano sei gia' loggato e vedi la spedizione in Italia
    link_domain: str = "ebay.it"

    # Fonti web (Vinted, Subito): pausa fra due richieste allo stesso sito e identita' del client
    scrape_delay: float = 2.0
    user_agent: str = ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0")

    def item_link(self, legacy_id: str, url: str = "") -> str:
        """Link all'annuncio. Gli ID delle fonti non eBay ("vinted:123") portano il proprio URL."""
        if ":" in legacy_id and url:
            return url
        return f"https://www.{self.link_domain}/itm/{legacy_id}"

    @property
    def ebay_api_base(self) -> str:
        return "https://api.sandbox.ebay.com" if self.ebay_env == "sandbox" else "https://api.ebay.com"


def load_config(env_file: str | None = None) -> Config:
    if load_dotenv is not None:
        load_dotenv(env_file or ".env", override=False)

    data_dir = Path(_str("SCOVATORE_DATA_DIR", "./data"))
    hunts_dir = Path(_str("SCOVATORE_HUNTS_DIR", "./cacce"))

    palantir = LLMConfig(
        name="palantir",
        base_url=_str("PALANTIR_BASE_URL", "http://palantir:8080/v1").rstrip("/"),
        api_key=_str("PALANTIR_API_KEY"),
        model=_str("PALANTIR_MODEL", "qwen3.5-4b"),
        tpm_limit=_int("PALANTIR_TPM_LIMIT", 0),
        json_mode=_bool("PALANTIR_JSON_MODE", True),
        reasoning_effort=_str("PALANTIR_REASONING_EFFORT"),
        temperature=_float("PALANTIR_TEMPERATURE", 0.2),
        timeout=_float("PALANTIR_TIMEOUT", 300),
        max_tokens=_int("PALANTIR_MAX_TOKENS", 1200),
    )
    groq = LLMConfig(
        name="groq",
        base_url=_str("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        api_key=_str("GROQ_API_KEY"),
        model=_str("GROQ_MODEL", "openai/gpt-oss-120b"),
        tpm_limit=_int("GROQ_TPM_LIMIT", 8000),
        json_mode=_bool("GROQ_JSON_MODE", True),
        reasoning_effort=_str("GROQ_REASONING_EFFORT", "low"),
        temperature=_float("GROQ_TEMPERATURE", 0.1),
        timeout=_float("GROQ_TIMEOUT", 120),
        max_tokens=_int("GROQ_MAX_TOKENS", 1500),
    )

    return Config(
        ebay_app_id=_str("EBAY_APP_ID"),
        ebay_cert_id=_str("EBAY_CERT_ID"),
        ebay_env=_str("EBAY_ENV", "production").lower(),
        ebay_buyer_country=_str("EBAY_BUYER_COUNTRY", "IT").upper(),
        ebay_buyer_zip=_str("EBAY_BUYER_ZIP", "10100"),
        ebay_daily_call_budget=_int("EBAY_DAILY_CALL_BUDGET", 4500),
        palantir=palantir,
        groq=groq,
        screen_batch_size=_int("SCOVATORE_SCREEN_BATCH", 8),
        description_max_chars=_int("SCOVATORE_DESC_MAX_CHARS", 2500),
        max_verify_per_run=_int("SCOVATORE_MAX_VERIFY_PER_RUN", 25),
        notify_min_score=_int("SCOVATORE_NOTIFY_MIN_SCORE", 70),
        data_dir=data_dir,
        hunts_dir=hunts_dir,
        db_path=Path(_str("SCOVATORE_DB", str(data_dir / "scovatore.db"))),
        ntfy_url=_str("SCOVATORE_NTFY_URL"),
        ntfy_token=_str("SCOVATORE_NTFY_TOKEN"),
        log_level=_str("SCOVATORE_LOG_LEVEL", "INFO").upper(),
        web_host=_str("SCOVATORE_WEB_HOST", "0.0.0.0"),
        web_port=_int("SCOVATORE_WEB_PORT", 8482),
        web_token=_str("SCOVATORE_WEB_TOKEN"),
        plan_llm=_plan_llm(),
        link_domain=_str("SCOVATORE_LINK_DOMAIN", "ebay.it").lower().removeprefix("www.").strip("/") or "ebay.it",
        scrape_delay=max(0.0, _float("SCOVATORE_SCRAPE_DELAY", 2.0)),
        user_agent=_str("SCOVATORE_USER_AGENT") or Config.user_agent,
    )


def _plan_llm() -> str:
    v = _str("SCOVATORE_PLAN_LLM", "palantir").lower()
    if v not in ("palantir", "groq"):
        raise ValueError(f"SCOVATORE_PLAN_LLM deve essere 'palantir' o 'groq', non {v!r}")
    return v
