import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen/qwen3.8-27b").strip()
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-large-v3-turbo").strip()

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_TEMPERATURE = 0.7
MAX_OUTPUT_TOKENS = 6000

HISTORY_MAX_CHARS = 20000
SEARCH_RESULTS = 4

TG_CHUNK = 3800
EDIT_INTERVAL = 1.1
