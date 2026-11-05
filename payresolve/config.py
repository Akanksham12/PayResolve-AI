import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
DB_PATH = Path(os.getenv("PAYRESOLVE_DB_PATH", PROJECT_ROOT / "data" / "payresolve.db"))
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
