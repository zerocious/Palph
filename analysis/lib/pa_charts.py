"""
Chart helpers for PA portfolio notebooks.

All functions return matplotlib Figure for easy savefig().
"""
from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def plot_retention_heatmap(cohorts: list[dict], title: str = "Cohort Retention") -> plt.Figure:
    """
    cohorts: [{"week": "2026-W20", "size": 12, "d1": 0.67, "d7": 0.25, "d30": None}, ...]
    """
    weeks = [c["week"] for c in cohorts]
    metrics = ["d1", "d7", "d30"]
    labels = ["D1", "D7", "D30"]

    data = []
    for c in cohorts:
        row = []
        for m in metrics:
            v = c.get(m)
            row.append(v * 100 if v is not None else np.nan)
        data.append(row)

    arr = np.array(data)
    fig, ax = plt.subplots(figsize=(8, max(3, len(weeks) * 0.4 + 1)))
    im = ax.imshow(arr, aspect="auto", cmap="YlGn", vmin=0, vmax=100)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels)
    ax.set_yticks(range(len(weeks)))
    ax.set_yticklabels([f"{w} (n={c['size']})" for w, c in zip(weeks, cohorts)])
    ax.set_title(title)

    for i in range(len(weeks)):
        for j in range(len(labels)):
            val = arr[i, j]
            if not np.isnan(val):
                ax.text(j, i, f"{val:.0f}%", ha="center", va="center", fontsize=9)

    fig.colorbar(im, ax=ax, label="Retention %")
    fig.tight_layout()
    return fig


def plot_funnel(steps: list[dict], title: str = "Activation Funnel") -> plt.Figure:
    """
    steps: [{"name": str, "count": int, "pct": float}, ...]
    """
    names = [s["name"] for s in steps]
    counts = [s["count"] for s in steps]
    pcts = [s["pct"] * 100 for s in steps]

    fig, ax = plt.subplots(figsize=(10, max(4, len(names) * 0.5)))
    y = range(len(names))
    bars = ax.barh(list(y), pcts, color="#4C72B0")
    ax.set_yticks(list(y))
    ax.set_yticklabels(names)
    ax.set_xlabel("% of registered")
    ax.set_title(title)
    ax.invert_yaxis()

    for bar, count, pct in zip(bars, counts, pcts):
        ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                f"{count} ({pct:.0f}%)", va="center", fontsize=8)

    fig.tight_layout()
    return fig


def plot_feature_adoption(features: list[dict], title: str = "Feature Adoption") -> plt.Figure:
    """
    features: [{"name": str, "count": int, "pct": float}, ...]
    """
    names = [f["name"] for f in features]
    pcts = [f["pct"] * 100 for f in features]

    fig, ax = plt.subplots(figsize=(10, max(4, len(names) * 0.4)))
    y = range(len(names))
    ax.barh(list(y), pcts, color="#55A868")
    ax.set_yticks(list(y))
    ax.set_yticklabels(names)
    ax.set_xlabel("Adoption %")
    ax.set_title(title)
    ax.invert_yaxis()
    fig.tight_layout()
    return fig


def plot_session_heatmap(grid: list[list[int]], weekday_labels: list[str],
                         hour_labels: list[str], title: str = "Session Heatmap") -> plt.Figure:
    """grid: 7 rows (weekdays) × 24 cols (hours) from /heatmap."""
    arr = np.array(grid)
    fig, ax = plt.subplots(figsize=(14, 5))
    im = ax.imshow(arr, aspect="auto", cmap="Blues")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels(hour_labels[::2])
    ax.set_yticks(range(7))
    ax.set_yticklabels(weekday_labels)
    ax.set_xlabel("Hour")
    ax.set_ylabel("Weekday")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="Events")
    fig.tight_layout()
    return fig
