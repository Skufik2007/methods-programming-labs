"""Агрегаты: статистики по (пациент, метрика), почасовые профили, минутные средние, эпизоды.

Все функции чистые: принимают DataFrame очищенных показаний, возвращают DataFrame.
"""

from __future__ import annotations

import polars as pl

from vitals.analytics.schema import AGG_JSON_SCHEMA, EPISODE_RULES, TS_DTYPE


def aggregate_stats(df: pl.DataFrame) -> pl.DataFrame:
    """SUM, AVG, MIN, MAX, COUNT по (patient_id, metric)."""
    return (
        df.group_by("patient_id", "metric")
        .agg(
            pl.col("value").sum().alias("sum"),
            pl.col("value").mean().alias("avg"),
            pl.col("value").min().alias("min"),
            pl.col("value").max().alias("max"),
            pl.len().alias("count"),
        )
        .sort("patient_id", "metric")
    )


def hourly_profile(df: pl.DataFrame) -> pl.DataFrame:
    """Почасовой профиль: среднее, мин, макс и число показаний по (patient_id, metric, час)."""
    return (
        df.with_columns(pl.col("ts").dt.truncate("1h").alias("hour"))
        .group_by("patient_id", "metric", "hour")
        .agg(
            pl.col("value").mean().alias("avg"),
            pl.col("value").min().alias("min"),
            pl.col("value").max().alias("max"),
            pl.len().alias("count"),
        )
        .sort("patient_id", "metric", "hour")
    )


def minute_stats(df: pl.DataFrame) -> pl.DataFrame:
    """Минутные статистики по (patient_id, metric): основа для правил эпизодов."""
    return (
        df.sort("ts", "seq")
        .with_columns(pl.col("ts").dt.truncate("1m").alias("minute"))
        .group_by("patient_id", "metric", "minute")
        .agg(
            pl.col("value").mean().alias("mean"),
            pl.col("value").min().alias("min"),
            pl.col("value").max().alias("max"),
            pl.col("value").last().alias("last"),
            pl.len().alias("count"),
        )
        .sort("patient_id", "metric", "minute")
    )


def find_episodes(minutes: pl.DataFrame) -> pl.DataFrame:
    """Эпизоды по правилам раздела 7 на минутных данных.

    Подряд идущие минуты, где условие выполнено, склеиваются в один эпизод. Для гипертонии берётся
    скользящее среднее минутных средних САД за 5 минут. `sensor_fault` здесь не считается: ему нужна доля
    невалидных, а очищенные данные их уже не содержат (долю дефектов даёт отчёт очистки).
    """
    empty = pl.DataFrame(
        schema={
            "alert": pl.String,
            "patient_id": pl.String,
            "start": TS_DTYPE,
            "end": TS_DTYPE,
            "minutes": pl.UInt32,
            "extreme": pl.Float64,
        }
    )
    parts: list[pl.DataFrame] = []
    for alert, metric, stat, op, threshold in EPISODE_RULES:
        sub = minutes.filter(pl.col("metric") == metric).sort("patient_id", "minute")
        if sub.is_empty():
            continue
        if stat == "mean5":
            level = pl.col("mean").rolling_mean_by("minute", window_size="5m").over("patient_id")
        else:
            level = pl.col(stat)
        sub = sub.with_columns(level.alias("level"))
        hit = sub.filter(pl.col("level") > threshold if op == ">" else pl.col("level") < threshold)
        if hit.is_empty():
            continue
        # новый эпизод начинается, если предыдущая минута пациента не подряд (разрыв > 1 мин)
        step = pl.col("minute").diff().over("patient_id")
        gap = step.is_null() | (step > pl.duration(minutes=1))
        hit = hit.with_columns(gap.cum_sum().over("patient_id").alias("episode"))
        extreme = pl.col("level").max() if op == ">" else pl.col("level").min()
        grouped = hit.group_by("patient_id", "episode").agg(
            pl.col("minute").min().alias("start"),
            (pl.col("minute").max() + pl.duration(minutes=1)).alias("end"),
            pl.len().cast(pl.UInt32).alias("minutes"),
            extreme.alias("extreme"),
        )
        parts.append(grouped.with_columns(pl.lit(alert).alias("alert")).select(empty.columns))
    if not parts:
        return empty
    return pl.concat(parts).sort("patient_id", "start", "alert")


def typed_windows(df: pl.DataFrame) -> pl.DataFrame:
    """Привести оконные агрегаты agg (JSONL или Flight) к типам: время -> Datetime[ms, UTC], счётчики -> Int64."""
    exprs = []
    for name, dtype in AGG_JSON_SCHEMA.items():
        if name in ("window_start", "window_end"):
            col = pl.col(name)
            if df.schema[name] == pl.String:
                exprs.append(col.str.to_datetime(time_unit="ms", time_zone="UTC", strict=False))
            else:
                exprs.append(col.cast(TS_DTYPE))
        else:
            exprs.append(pl.col(name).cast(dtype, strict=False))
    return df.select(exprs)
