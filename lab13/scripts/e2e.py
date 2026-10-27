"""Сквозная проверка работающего стенда (compose в CI или dev.ps1 локально).

Зависимости: стандартная библиотека и httpx. Запуск:

    python scripts/e2e.py --phase before          # обращения, трасса, масштабирование, снимок Redis
    docker compose restart reminder               # (делает CI-шаг)
    python scripts/e2e.py --phase after           # счётчики Redis после перезапуска не обнулились

Код возврата: 0 — все проверки пройдены, 1 — есть провалы, 2 — ошибка запуска.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Обращения для проверки: индекс RED_FLAG — жалоба с красным флагом, цепочка должна остановиться.
CASES = [
    ("Иванова Анна Петровна", 34, "Третий день температура 38, кашель, болит горло"),
    ("Петров Сергей Иванович", 51, "Давно беспокоят головные боли по вечерам и головокружение при вставании"),
    ("Сидорова Мария Олеговна", 47, "Сильная боль в груди отдаёт в левую руку, тяжело дышать, холодный пот"),
    ("Кузнецов Дмитрий Алексеевич", 29, "Подвернул ногу на тренировке, опухла лодыжка, больно наступать"),
    ("Смирнова Елена Викторовна", 62, "Появилась сыпь и сильный зуд на руках после смены стирального порошка"),
]
RED_FLAG = 2
MIN_TRACE_SERVICES = 5
EXPECTED_SERVICES = ("orchestrator", "llm-agent", "agent-triage", "agent-booking", "agent-reminder", "agent-feedback")
REQUIRED_ROLES = ("intake", "triage", "booking", "reminder", "feedback")
LOAD_COUNT = 40


class Report:
    """Накопитель результатов: печатает строку на каждую проверку и считает провалы."""

    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def check(self, ok: bool, title: str, detail: str = "") -> bool:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {title}" + (f" — {detail}" if detail else ""), flush=True)
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok

    def info(self, text: str) -> None:
        print(f"      {text}", flush=True)


def wait_until(predicate: Any, timeout: float, interval: float = 1.0) -> Any:
    """Опрашивает predicate до первого «истинного» значения; None, если время вышло."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = predicate()
        except (httpx.HTTPError, OSError, ValueError, KeyError):
            value = None
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# ---- минимальный клиент Redis (RESP2) ---------------------------------------------------------------


class Redis:
    """Только то, что нужно проверке (SELECT, HGETALL); чтобы не тянуть зависимость redis-py."""

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), timeout=5)
        self.buf = self.sock.makefile("rb")
        db = (parsed.path or "/0").lstrip("/") or "0"
        if db != "0":
            self.command("SELECT", db)

    def close(self) -> None:
        self.buf.close()
        self.sock.close()

    def command(self, *args: str) -> Any:
        payload = f"*{len(args)}\r\n".encode()
        for arg in args:
            raw = arg.encode()
            payload += b"$" + str(len(raw)).encode() + b"\r\n" + raw + b"\r\n"
        self.sock.sendall(payload)
        return self._read()

    def _read(self) -> Any:
        line = self.buf.readline()
        if not line:
            raise ConnectionError("Redis закрыл соединение")
        kind, rest = line[:1], line[1:].rstrip(b"\r\n")
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise ValueError(f"Redis: {rest.decode()}")
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            if size < 0:
                return None
            data = self.buf.read(size + 2)
            return data[:size].decode()
        if kind == b"*":
            size = int(rest)
            return None if size < 0 else [self._read() for _ in range(size)]
        raise ValueError(f"неизвестный ответ Redis: {line!r}")

    def hgetall(self, key: str) -> dict[str, str]:
        flat = self.command("HGETALL", key) or []
        return dict(zip(flat[::2], flat[1::2], strict=True))


# ---- разбор ответа Jaeger -----------------------------------------------------------------------------


def services_from_trace(data: dict[str, Any]) -> set[str]:
    """Имена сервисов из ответа Jaeger: формат v3 (OTLP JSON) или v1 (processes)."""
    names: set[str] = set()
    result = data.get("result") or {}
    for resource_spans in result.get("resourceSpans", []):
        for attr in (resource_spans.get("resource") or {}).get("attributes", []):
            if attr.get("key") == "service.name":
                value = (attr.get("value") or {}).get("stringValue")
                if value:
                    names.add(value)
    for trace in data.get("data", []):
        for process in (trace.get("processes") or {}).values():
            if process.get("serviceName"):
                names.add(process["serviceName"])
    return names


def fetch_trace_services(client: httpx.Client, jaeger_url: str, trace_id: str) -> set[str]:
    base = jaeger_url.rstrip("/")
    for path in (f"/api/v3/traces/{trace_id}", f"/api/traces/{trace_id}"):
        response = client.get(base + path)
        if response.status_code == 200:
            names = services_from_trace(response.json())
            if names:
                return names
    return set()


# ---- проверки ------------------------------------------------------------------------------------------


class E2E:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rep = Report()
        self.client = httpx.Client(timeout=10)
        self.dash = args.base_url.rstrip("/")

    def get(self, path: str) -> Any:
        response = self.client.get(self.dash + path)
        response.raise_for_status()
        return response.json()

    def online_roles(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for agent in self.get("/api/agents")["agents"]:
            # В API дашборда нет поля online: «в сети» — heartbeat моложе 15 с (раздел 9 контракта).
            if float(agent.get("age_s", 1e9)) < 15 and agent.get("status") != "stopping":
                counts[agent["role"]] = counts.get(agent["role"], 0) + 1
        return counts

    def wait_agents(self) -> bool:
        def ready() -> dict[str, int] | None:
            roles = self.online_roles()
            return roles if all(roles.get(r, 0) >= 1 for r in REQUIRED_ROLES) else None

        roles = wait_until(ready, self.args.timeout)
        return self.rep.check(
            roles is not None,
            "все агенты в сети (heartbeat)",
            ", ".join(f"{k}={v}" for k, v in sorted((roles or self.online_roles()).items())),
        )

    # -- этап 1: обращения и трасса

    def check_cases(self) -> str:
        """Отправляет 5 обращений, ждёт завершения; возвращает trace_id завершённого запуска."""
        run_ids: list[str] = []
        for index, (name, age, complaint) in enumerate(CASES):
            body = {
                "patient_name": name,
                "age": age,
                "phone": f"+7 900 111-{index + 1:04d}",
                "complaint": complaint,
            }
            response = self.client.post(self.dash + "/api/cases", json=body)
            if response.status_code != 202:
                self.rep.check(
                    False, f"POST /api/cases #{index + 1}", f"HTTP {response.status_code}: {response.text[:200]}"
                )
                return ""
            run_ids.append(response.json()["run_id"])
        self.rep.check(True, "5 обращений приняты (POST /api/cases → 202)")

        def finished() -> list[dict[str, Any]] | None:
            runs = [self.get(f"/api/runs/{rid}") for rid in run_ids]
            return runs if all(r["run"]["status"] != "running" for r in runs) else None

        runs = wait_until(finished, self.args.timeout, interval=1.5)
        if runs is None:
            self.rep.check(False, "обращения завершились", f"за {self.args.timeout:.0f} с не все вышли из running")
            return ""
        trace_id = ""
        for index, run in enumerate(runs):
            status = run["run"]["status"]
            want = "stopped" if index == RED_FLAG else "completed"
            ok = self.rep.check(
                status == want,
                f"обращение #{index + 1}: статус {status} (ожидался {want})",
                run["run"].get("error") or run["run"].get("recommendation", ""),
            )
            if ok and want == "completed" and not trace_id:
                trace_id = run["run"].get("trace_id", "")
        if not trace_id:
            self.rep.check(False, "у завершённого запуска есть trace_id")
        return trace_id

    def check_trace(self, trace_id: str) -> None:
        if not trace_id:
            self.rep.check(False, "трасса в Jaeger", "нет trace_id — проверка пропущена")
            return

        def enough() -> set[str] | None:
            names = fetch_trace_services(self.client, self.args.jaeger_url, trace_id)
            return names if len(names & set(EXPECTED_SERVICES)) >= MIN_TRACE_SERVICES else None

        # Экспортёры отправляют спаны пакетами раз в несколько секунд, поэтому ждём.
        found = wait_until(enough, self.args.trace_timeout, interval=2)
        names = found or fetch_trace_services(self.client, self.args.jaeger_url, trace_id)
        self.rep.check(
            found is not None,
            f"трасса {trace_id[:8]}…: не менее {MIN_TRACE_SERVICES} сервисов в одной трассе",
            "сервисы: " + (", ".join(sorted(names)) or "трасса не найдена"),
        )

    # -- этап 2: автомасштабирование

    def autoscaler_triage(self) -> dict[str, Any] | None:
        """Состояние шага triage: напрямую из /status автомасштабировщика, иначе через дашборд."""
        steps: list[dict[str, Any]] = []
        try:
            response = self.client.get(self.args.autoscaler_url.rstrip("/") + "/status")
            response.raise_for_status()
            steps = response.json().get("steps", [])
        except (httpx.HTTPError, ValueError):
            data = self.get("/api/autoscaler")
            steps = data.get("steps", []) if data.get("available") else []
        return next((s for s in steps if s.get("step") == "triage"), None)

    def check_scaling(self) -> list[str]:
        response = self.client.post(self.dash + "/api/load", json={"count": LOAD_COUNT})
        if not self.rep.check(
            response.status_code == 202,
            f"нагрузка {LOAD_COUNT} обращений (POST /api/load)",
            f"HTTP {response.status_code}",
        ):
            return []
        run_ids: list[str] = response.json()["run_ids"]
        peak = {"total": 0, "backlog": 0}

        def scaled() -> dict[str, Any] | None:
            state = self.autoscaler_triage()
            if state is None:
                return None
            peak["total"] = max(peak["total"], state.get("total", 0), len(state.get("auto") or []) + 1)
            peak["backlog"] = max(peak["backlog"], state.get("backlog", 0))
            return state if (state.get("total", 0) > 1 or state.get("auto")) else None

        state = wait_until(scaled, self.args.scale_timeout, interval=1)
        self.rep.check(
            state is not None,
            "автомасштабировщик: /status показывает больше одного экземпляра триажа",
            f"макс. очередь {peak['backlog']}, макс. экземпляров {peak['total']}"
            + (f", добавочные: {', '.join(state.get('auto') or [])}" if state else ""),
        )

        def event_seen() -> bool:
            return self.get("/api/events?action=scaled_up&limit=10")["total"] > 0

        self.rep.check(
            bool(wait_until(event_seen, 20)),
            "в журнале дашборда есть событие scaled_up",
        )
        return run_ids

    def wait_load_done(self, run_ids: list[str]) -> None:
        if not run_ids:
            return

        def done() -> dict[str, int] | None:
            counts: dict[str, int] = {}
            for rid in run_ids:
                status = self.get(f"/api/runs/{rid}")["run"]["status"]
                counts[status] = counts.get(status, 0) + 1
            return counts if counts.get("running", 0) == 0 else None

        counts = wait_until(done, self.args.timeout * 2, interval=3)
        errors: dict[str, int] = {}
        for rid in run_ids:
            run = self.get(f"/api/runs/{rid}")["run"]
            if run["status"] == "failed":
                errors[run["error"][:80]] = errors.get(run["error"][:80], 0) + 1
        # Провалившийся запуск — это пациент без записи и без рекомендации: так быть не должно.
        self.rep.check(
            counts is not None and not errors,
            "обращения нагрузки обработаны без провалов",
            f"{counts or 'не все завершились'}; ошибки: {errors or 'нет'}",
        )

    # -- этап 3: Redis

    def redis_stats(self) -> dict[str, int]:
        redis = Redis(self.args.redis)
        try:
            raw = redis.hgetall("mas:stats:reminder")
        finally:
            redis.close()
        return {k: int(v) for k, v in raw.items() if v.lstrip("-").isdigit()}

    def phase_before(self) -> None:
        self.wait_agents()
        trace_id = self.check_cases()
        self.check_trace(trace_id)
        load_runs = self.check_scaling()
        self.wait_load_done(load_runs)
        # Даём напоминаниям отправиться (REMINDER_DEMO_DELAY) и счётчикам записаться в Redis.
        time.sleep(self.args.settle)
        stats = self.redis_stats()
        self.rep.check(
            stats.get("processed", 0) >= len(CASES) - 1, "Redis: mas:stats:reminder заполнен", json.dumps(stats)
        )
        Path(self.args.state_file).write_text(json.dumps({"reminder": stats}), encoding="utf-8")
        self.rep.info(f"состояние сохранено в {self.args.state_file}")

    def phase_after(self) -> None:
        try:
            before = json.loads(Path(self.args.state_file).read_text(encoding="utf-8"))["reminder"]
        except (OSError, ValueError, KeyError):
            self.rep.check(False, "файл состояния фазы before", f"не прочитан: {self.args.state_file}")
            return
        roles = wait_until(lambda: self.online_roles().get("reminder"), self.args.timeout)
        self.rep.check(bool(roles), "агент reminder снова в сети после перезапуска")
        after = self.redis_stats()
        self.rep.info(f"до: {before}; после: {after}")
        self.rep.check(after.get("processed", 0) > 0, "счётчик processed не обнулился после перезапуска")
        regressions = {k: (v, after.get(k, 0)) for k, v in before.items() if after.get(k, 0) < v}
        self.rep.check(
            not regressions, "счётчики mas:stats:reminder не уменьшились", str(regressions) if regressions else ""
        )

        # Счётчик продолжается: новая задача увеличивает значение, а не начинает с нуля.
        name, age, complaint = CASES[0]
        body = {"patient_name": name, "age": age, "phone": "+7 900 222-0001", "complaint": complaint}
        response = self.client.post(self.dash + "/api/cases", json=body)
        response.raise_for_status()
        run_id = response.json()["run_id"]
        status = wait_until(
            lambda: (s := self.get(f"/api/runs/{run_id}")["run"]["status"]) != "running" and s,
            self.args.timeout,
            interval=1.5,
        )
        self.rep.check(status == "completed", "после перезапуска новое обращение проходит всю цепочку", str(status))
        time.sleep(self.args.settle / 2)
        final = self.redis_stats()
        self.rep.check(
            final.get("processed", 0) > before.get("processed", 0),
            "счётчик processed продолжился с сохранённого значения",
            f"{before.get('processed', 0)} → {final.get('processed', 0)}",
        )

    def run(self) -> int:
        print(f"e2e: фаза {self.args.phase}, дашборд {self.dash}")
        try:
            if self.args.phase == "before":
                self.phase_before()
            else:
                self.phase_after()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self.rep.check(False, "проверка прервана ошибкой", f"{type(exc).__name__}: {exc}")
        finally:
            self.client.close()
        print(f"итого: пройдено {self.rep.passed}, провалено {self.rep.failed}")
        return 1 if self.rep.failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Сквозная проверка стенда MAS")
    p.add_argument(
        "--phase",
        choices=("before", "after"),
        default="before",
        help="before — основная часть; after — после перезапуска reminder",
    )
    p.add_argument("--base-url", default="http://localhost:8000", help="адрес дашборда")
    p.add_argument("--jaeger-url", default="http://localhost:16686", help="адрес Jaeger (UI и API запросов)")
    p.add_argument("--autoscaler-url", default="http://localhost:8090", help="адрес /status автомасштабировщика")
    p.add_argument("--redis", default="redis://localhost:6379/0", help="адрес Redis")
    p.add_argument("--state-file", default="e2e-state.json", help="файл состояния между фазами")
    p.add_argument("--timeout", type=float, default=120, help="ожидание готовности/завершения запусков, с")
    p.add_argument("--trace-timeout", type=float, default=60, help="ожидание появления трассы в Jaeger, с")
    p.add_argument("--scale-timeout", type=float, default=60, help="ожидание масштабирования после нагрузки, с")
    p.add_argument("--settle", type=float, default=12, help="пауза перед снимком счётчиков Redis, с")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return E2E(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
