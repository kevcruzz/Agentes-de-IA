"""
shared/utils.py — Utilitários compartilhados por todos os agentes

Inclui:
- Retry com backoff exponencial
- Logger estruturado (JSON)
- Configuração centralizada
- Decorador de timeout
"""

import os
import json
import time
import logging
import functools
from datetime import datetime
from typing import Any, Callable, Optional
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIGURAÇÃO ────────────────────────────────────────────────────────────

class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    MODEL: str = os.getenv("MODEL", "claude-sonnet-4-20250514")
    MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "2048"))
    MAX_ITERATIONS: int = int(os.getenv("MAX_ITERATIONS", "15"))
    TIMEOUT_SECONDS: int = int(os.getenv("TIMEOUT_SECONDS", "30"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./agentes.db")

config = Config()

# ─── LOGGER ESTRUTURADO ──────────────────────────────────────────────────────

class StructuredLogger:
    """Logger que emite JSON — fácil de indexar no Datadog, CloudWatch, etc."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.logger = logging.getLogger(agent_name)
        self.logger.setLevel(getattr(logging, config.LOG_LEVEL))

        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    def _log(self, level: str, event: str, **kwargs):
        record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "agent": self.agent_name,
            "level": level,
            "event": event,
            **kwargs,
        }
        getattr(self.logger, level.lower())(json.dumps(record, ensure_ascii=False))

    def info(self, event: str, **kwargs):
        self._log("INFO", event, **kwargs)

    def warning(self, event: str, **kwargs):
        self._log("WARNING", event, **kwargs)

    def error(self, event: str, **kwargs):
        self._log("ERROR", event, **kwargs)

    def debug(self, event: str, **kwargs):
        self._log("DEBUG", event, **kwargs)


# ─── RETRY COM BACKOFF EXPONENCIAL ───────────────────────────────────────────

def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
):
    """
    Decorador de retry com backoff exponencial.
    Tenta novamente em 1s, 2s, 4s antes de desistir.

    Uso:
        @retry(max_attempts=3, exceptions=(anthropic.APIError,))
        async def chamar_api():
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            logger = StructuredLogger("retry")
            last_exception = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        logger.error(
                            "max_retries_reached",
                            func=func.__name__,
                            attempts=attempt,
                            error=str(e),
                        )
                        raise

                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "retrying",
                        func=func.__name__,
                        attempt=attempt,
                        delay_seconds=delay,
                        error=str(e),
                    )
                    time.sleep(delay)

            raise last_exception
        return wrapper
    return decorator


# ─── MEDIDOR DE TEMPO ────────────────────────────────────────────────────────

class Timer:
    """Mede e loga duração de operações."""

    def __init__(self, logger: StructuredLogger, operation: str):
        self.logger = logger
        self.operation = operation
        self.start = None

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args):
        duration_ms = round((time.perf_counter() - self.start) * 1000, 2)
        self.logger.info(
            "operation_completed",
            operation=self.operation,
            duration_ms=duration_ms,
        )


# ─── FORMATADOR DE RESPOSTA DE ERRO ──────────────────────────────────────────

def error_response(message: str, details: Optional[str] = None) -> dict:
    return {
        "success": False,
        "error": message,
        "details": details,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def success_response(data: Any, message: str = "ok") -> dict:
    return {
        "success": True,
        "message": message,
        "data": data,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
