"""LLM-агент: разбор обращений и ответы на отзывы через Ollama с запасными правилами."""

from mas.llm_agent.agent import LLMAgent
from mas.llm_agent.fallback import fallback_classify, fallback_reply

__all__ = ["LLMAgent", "fallback_classify", "fallback_reply"]
