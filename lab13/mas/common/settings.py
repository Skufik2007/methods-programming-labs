"""Настройки процессов из переменных окружения."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LLM_STEPS = frozenset({"intake", "feedback_reply"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    nats_url: str = "nats://localhost:4222"
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_sdk_disabled: bool = False
    log_level: str = "INFO"
    log_file: str = ""

    # Раздел 5: таймаут шага, для LLM-шагов больше; попыток не больше 3.
    step_timeout_s: float = 15.0
    llm_step_timeout_s: float = 90.0
    max_attempts: int = 3

    # Раздел 6: окно сбора ставок 500 мс, до 3 раундов при slot_taken.
    auction_window_s: float = 0.5
    book_timeout_s: float = 5.0
    max_auction_rounds: int = 3
    # Раздел 6, п. 3: при no_bids плановое обращение получает второй аукцион с окном в N раз шире.
    no_bids_extend_factor: float = 4.0

    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:1.5b"
    ollama_timeout_s: float = 60.0
    llm_mode: Literal["llm", "fallback"] = "llm"
    agent_id: str = ""

    def step_timeout(self, step: str) -> float:
        return self.llm_step_timeout_s if step in LLM_STEPS else self.step_timeout_s


def get_settings() -> Settings:
    """Фабрика: настройки читаются при вызове, а не при импорте."""
    return Settings()
