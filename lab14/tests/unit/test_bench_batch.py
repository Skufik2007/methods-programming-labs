"""Находка №5: замер сборщиков при BATCH_SIZE=500 упирался в fsync после каждого пакета и
сравнивал скорость диска, а не Go и Python (Go: 72 тыс. показаний/с при 500 против 446 тыс.
при 20 000). Бенчмарк обязан запускать сборщики с большим пакетом."""

from pathlib import Path

from vitals.bench import collectors, procs


def test_collectors_run_with_large_batch(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def fake_run(cmd, env, timeout):  # noqa: ARG001
        seen.update(env)
        return procs.Measured(0, 1.0, 10.0, 0.5)

    monkeypatch.setattr(procs, "run_measured", fake_run)
    collectors.one_run("go", "http://localhost:1", 10, 1.0, 4, 19999)
    assert int(seen["BATCH_SIZE"]) >= 5_000
    assert seen["SINKS"] == "files"
