from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    data_dir: Path
    web_port: int
    web_host: str
    public_base_url: str
    offer_hours: int
    max_init_age: int


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise RuntimeError('BOT_TOKEN is missing. Set BOT_TOKEN in BotHost environment.')
    data_dir = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    url = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if url and not url.startswith('https://'):
        raise RuntimeError('PUBLIC_BASE_URL must be HTTPS (or empty).')
    hours = int(os.getenv('OFFER_LIFETIME_HOURS', '6'))
    if hours < 1 or hours > 72:
        raise RuntimeError('OFFER_LIFETIME_HOURS must be between 1 and 72.')
    return Settings(
        bot_token=token,
        data_dir=data_dir,
        web_port=int(os.getenv('PORT', os.getenv('WEB_PORT', '3000'))),
        web_host=os.getenv('WEB_HOST', '0.0.0.0'),
        public_base_url=url,
        offer_hours=hours,
        max_init_age=int(os.getenv('INIT_DATA_MAX_AGE_SECONDS', '3600')),
    )
