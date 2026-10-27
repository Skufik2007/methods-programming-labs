"""Графики раздела 9.6: Plotly (интерактивный HTML) и matplotlib (PNG).

Подготовка данных вынесена в чистые функции (`heatmap_data`, `defect_counts`, `pick_patient`),
рисование — в `make_plots`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

DEFECT_LABELS = {
    "duplicates": "дубликаты (device_id, seq)",
    "unit_fixed": "неверная единица",
    "future_ts": "ts из будущего",
    "out_of_range": "вне диапазона",
    "missing_value": "пропуск value",
}


def defect_counts(report: list[dict[str, Any]]) -> dict[str, int]:
    """Число строк по типам дефектов из отчёта очистки."""
    by_step = {r["step"]: r for r in report}

    def affected(step: str) -> int:
        return int(by_step[step]["affected"]) if step in by_step else 0

    detail = by_step.get("validate", {}).get("detail", {})
    invalid = affected("validate")
    return {
        "duplicates": affected("drop_duplicates"),
        "unit_fixed": affected("normalize_unit") + int(detail.get("bad_unit", 0)),
        "future_ts": affected("drop_future"),
        "out_of_range": int(detail.get("out_of_range", invalid - int(detail.get("bad_unit", 0)))),
        "missing_value": affected("drop_missing"),
    }


def pick_patient(clean: pl.DataFrame, episodes: pl.DataFrame) -> str | None:
    """Пациент для графика: с наибольшим числом эпизодов, иначе с наибольшим числом показаний ЧСС."""
    if not episodes.is_empty():
        counts = episodes.group_by("patient_id").len()
        return counts.sort("len", "patient_id", descending=[True, False])["patient_id"][0]
    hr = clean.filter(pl.col("metric") == "heart_rate")
    if hr.is_empty():
        return None
    return hr.group_by("patient_id").len().sort("len", "patient_id", descending=[True, False])["patient_id"][0]


ALERT_METRIC = {"tachycardia": "heart_rate", "desaturation": "spo2", "fever": "temperature",
                "hypertension": "systolic_bp"}
PANEL_TITLES = {"heart_rate": "ЧСС, уд/мин", "spo2": "SpO2, %", "temperature": "Температура, °C",
                "systolic_bp": "САД, мм рт. ст."}


def timeseries_panels(episodes: pl.DataFrame, patient: str) -> list[str]:
    """Две панели графика пациента: метрики его эпизодов (в порядке появления), дополненные ЧСС/SpO2.

    Раньше всегда рисовались ЧСС и SpO2, и полоса эпизода лихорадки закрывала графики без
    отклонений (находка №6).
    """
    own = episodes.filter(pl.col("patient_id") == patient)["alert"].to_list() if not episodes.is_empty() else []
    panels: list[str] = []
    for metric in [ALERT_METRIC[a] for a in own if a in ALERT_METRIC] + ["heart_rate", "spo2"]:
        if metric not in panels:
            panels.append(metric)
    return panels[:2]


def heatmap_data(clean: pl.DataFrame, metric: str = "heart_rate") -> tuple[list[str], list[Any], np.ndarray, str]:
    """Матрица «пациент x интервал времени»: среднее значение метрики.

    Размер интервала зависит от длины данных: час (сутки), 10 минут (несколько часов), минута (короткий прогон).
    """
    sub = clean.filter(pl.col("metric") == metric)
    if sub.is_empty():
        return [], [], np.zeros((0, 0)), "1m"
    span = sub["ts"].max() - sub["ts"].min()
    every = "1h" if span >= timedelta(hours=6) else ("10m" if span > timedelta(minutes=30) else "1m")
    grid = (
        sub.with_columns(pl.col("ts").dt.truncate(every).alias("bin"))
        .group_by("patient_id", "bin")
        .agg(pl.col("value").mean().alias("avg"))
    )
    patients = sorted(grid["patient_id"].unique().to_list())
    bins = sorted(grid["bin"].unique().to_list())
    p_idx = {p: i for i, p in enumerate(patients)}
    b_idx = {b: i for i, b in enumerate(bins)}
    matrix = np.full((len(patients), len(bins)), np.nan)
    for p, b, v in grid.select("patient_id", "bin", "avg").iter_rows():
        matrix[p_idx[p], b_idx[b]] = v
    return patients, bins, matrix, every


def make_plots(
    clean: pl.DataFrame,
    episodes: pl.DataFrame,
    report: list[dict[str, Any]],
    out_dir: str | Path,
) -> list[str]:
    """Нарисовать 4 графика; вернуть список созданных файлов (HTML и PNG)."""
    import matplotlib

    matplotlib.use("Agg")  # без GUI: конвейер запускается и на сервере
    import matplotlib.pyplot as plt
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    def save_html(fig: Any, name: str) -> None:
        path = out / f"{name}.html"
        fig.write_html(path, include_plotlyjs="directory")  # plotly.min.js кладётся рядом один раз
        created.append(path.name)

    def save_png(fig: Any, name: str) -> None:
        path = out / f"{name}.png"
        fig.savefig(path, dpi=130, bbox_inches="tight")
        plt.close(fig)
        created.append(path.name)

    # 1. Временной ряд пациента: метрики его эпизодов, полоса эпизода — на панели своей метрики
    patient = pick_patient(clean, episodes)
    if patient is not None:
        panels = timeseries_panels(episodes, patient)
        series = {
            m: clean.filter((pl.col("patient_id") == patient) & (pl.col("metric") == m)).sort("ts") for m in panels
        }
        last_ts = max((series[m]["ts"].max() for m in panels if series[m].height), default=None)
        spans = [
            (s, min(e, last_ts) if last_ts is not None else e, alert)
            for s, e, alert in episodes.filter(pl.col("patient_id") == patient)
            .select("start", "end", "alert")
            .iter_rows()
        ]
        titles = {m: PANEL_TITLES[m] for m in panels}
        heading = f"Пациент {patient}: {', '.join(titles.values())} и эпизоды"
        fig = make_subplots(rows=len(panels), cols=1, shared_xaxes=True, subplot_titles=list(titles.values()))
        for row, m in enumerate(panels, start=1):
            trace = go.Scatter(x=series[m]["ts"], y=series[m]["value"], mode="lines", name=titles[m])
            fig.add_trace(trace, row=row, col=1)
            for s, e, alert in spans:
                if ALERT_METRIC.get(alert) == m:
                    fig.add_vrect(x0=s, x1=e, fillcolor="red", opacity=0.2, line_width=0,
                                  annotation_text=alert, row=row, col=1)
        fig.update_layout(title=heading, height=600)
        save_html(fig, "timeseries")
        mfig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(10, 6), squeeze=False)
        for ax, m in zip(axes[:, 0], panels, strict=True):
            ax.plot(series[m]["ts"].to_list(), series[m]["value"].to_list(), lw=0.8)
            ax.set_ylabel(titles[m])
            for s, e, alert in spans:
                if ALERT_METRIC.get(alert) == m:
                    ax.axvspan(s, e, color="red", alpha=0.2)
                    ax.annotate(alert, (s, ax.get_ylim()[1]), fontsize=7, va="top")
        axes[0, 0].set_title(heading)
        mfig.autofmt_xdate()
        save_png(mfig, "timeseries")

    # 2. Гистограмма распределения ЧСС
    hr = clean.filter(pl.col("metric") == "heart_rate")["value"]
    fig = go.Figure(go.Histogram(x=hr, nbinsx=40))
    fig.update_layout(title="Распределение ЧСС", xaxis_title="уд/мин", yaxis_title="число показаний")
    save_html(fig, "hist_heart_rate")
    mfig, ax = plt.subplots(figsize=(8, 4.5))
    ax.hist(hr.to_numpy(), bins=40)
    ax.set(title="Распределение ЧСС", xlabel="уд/мин", ylabel="число показаний")
    save_png(mfig, "hist_heart_rate")

    # 3. Тепловая карта «пациент x время»
    patients, bins, matrix, every = heatmap_data(clean, "heart_rate")
    if patients:
        fig = go.Figure(go.Heatmap(z=matrix, x=bins, y=patients, colorscale="RdYlBu_r", colorbar={"title": "уд/мин"}))
        fig.update_layout(title=f"Средняя ЧСС: пациент x интервал {every}", height=max(400, 14 * len(patients)))
        save_html(fig, "heatmap_heart_rate")
        mfig, ax = plt.subplots(figsize=(9, max(4, 0.18 * len(patients))))
        im = ax.imshow(matrix, aspect="auto", cmap="RdYlBu_r")
        ax.set_yticks(range(0, len(patients), max(1, len(patients) // 25)))
        ax.set_yticklabels(patients[:: max(1, len(patients) // 25)], fontsize=6)
        step = max(1, len(bins) // 8)
        ax.set_xticks(range(0, len(bins), step))
        ax.set_xticklabels([b.strftime("%H:%M") for b in bins[::step]], fontsize=7)
        ax.set_title(f"Средняя ЧСС: пациент x интервал {every}")
        mfig.colorbar(im, label="уд/мин")
        save_png(mfig, "heatmap_heart_rate")

    # 4. Доля дефектов по типам (относительно строк до очистки)
    counts = defect_counts(report)
    total = report[0]["rows_before"] if report else 0
    labels = [DEFECT_LABELS[k] for k in counts]
    shares = [100.0 * v / total if total else 0.0 for v in counts.values()]
    fig = go.Figure(go.Bar(x=labels, y=shares, text=[f"{v}" for v in counts.values()]))
    fig.update_layout(title="Доля дефектов по типам", yaxis_title="% строк до очистки")
    save_html(fig, "defects")
    mfig, ax = plt.subplots(figsize=(8, 4.5))
    bars = ax.bar(range(len(labels)), shares)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    ax.bar_label(bars, labels=[str(v) for v in counts.values()], fontsize=8)
    ax.set(title="Доля дефектов по типам", ylabel="% строк до очистки")
    save_png(mfig, "defects")
    return created
