"""Графики замеров (matplotlib, PNG в reports/img/). Бэкенд Agg: окно не нужно."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"go": "#00ADD8", "python": "#FFB000", "jsonl": "#8E6BBF", "flight": "#2CA02C",
          "raw": "#D62728", "agg": "#1F77B4", "rust": "#DE6A2C", "polars": "#4C78A8"}


def grouped_bars(ax, categories: Sequence[str], series: dict[str, Sequence[float]], ylabel: str, title: str,
                 log: bool = False) -> None:
    n = max(len(series), 1)
    width = 0.8 / n
    for i, (name, vals) in enumerate(series.items()):
        xs = [j + (i - (n - 1) / 2) * width for j in range(len(categories))]
        bars = ax.bar(xs, vals, width, label=name, color=COLORS.get(name))
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(f"{v:,.0f}" if v >= 100 else f"{v:.2f}", (b.get_x() + b.get_width() / 2, v),
                        ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(categories)), categories, fontsize=8)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)
    if log:
        ax.set_yscale("log")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)


def save_figure(fig, name: str, img_dir: Path) -> Path:
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / name
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def panels(n: int, width: float = 5.0, height: float = 4.0):
    fig, axes = plt.subplots(1, n, figsize=(width * n, height))
    return fig, ([axes] if n == 1 else list(axes))
