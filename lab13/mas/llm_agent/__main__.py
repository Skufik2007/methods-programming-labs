"""Запуск: `python -m mas.llm_agent --role roles/intake.md`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from mas.common.bus import NatsBus
from mas.common.logging import setup_logging
from mas.common.roles import RoleError, load_role
from mas.common.settings import get_settings
from mas.common.telemetry import create_tracer_provider
from mas.llm_agent.agent import SUPPORTED_STEPS, LLMAgent
from mas.llm_agent.ollama import OllamaClient

log = logging.getLogger("mas.llm_agent.cli")


async def _run(role_path: str) -> int:
    settings = get_settings()
    try:
        role = load_role(role_path)
    except RoleError as exc:
        print(f"ошибка роли: {exc}", file=sys.stderr)
        return 2
    if role.step not in SUPPORTED_STEPS:
        print(
            f"ошибка роли: шаг {role.step!r} не поддерживается LLM-агентом ({', '.join(SUPPORTED_STEPS)})",
            file=sys.stderr,
        )
        return 2

    provider = create_tracer_provider("llm-agent", settings)
    bus = NatsBus(settings.nats_url, name=f"llm-{role.step}")
    ollama = OllamaClient(settings.ollama_url, float(role.params.get("timeout_s", settings.ollama_timeout_s)))
    try:
        await bus.connect()
        await bus.ensure_stream()
        agent = LLMAgent(bus, role, settings, ollama, provider.get_tracer("llm-agent"))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, agent.request_stop)
            except NotImplementedError:  # Windows: остаётся KeyboardInterrupt
                pass
        log.info("агент %s запущен, шаг %s, режим %s", agent.agent_id, role.step, settings.llm_mode)
        await agent.run()
        return 0
    finally:
        await ollama.aclose()
        await bus.close()
        await asyncio.to_thread(provider.shutdown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mas.llm_agent")
    parser.add_argument("--role", required=True, help="путь к файлу роли, например roles/intake.md")
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)
    try:
        return asyncio.run(_run(args.role))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        log.error("не удалось подключиться к NATS (%s): %s", settings.nats_url, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
