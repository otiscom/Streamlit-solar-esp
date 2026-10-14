"""ESP32 -> Firebase RTDB: historia temperatur i biezace odczyty.

Zgodne z dotychczasowymi rekordami oraz szkicem MonitorWody v2.1.
Zakresy liczymy w UTC; daty wyswietlamy zawsze w Europe/Warsaw.
"""
import json

import altair as alt
import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DB = "https://solar-esp-rtdb-default-rtdb.europe-west1.firebasedatabase.app"
LOGS, STAT = "/logs", "/log"
TZ = "Europe/Warsaw"
REFRESH_MS = 60_000
STALE_MINUTES = 3  # normalny pomiar ESP co minute
GAP_MS = 3 * 60_000
MAX_CHART_POINTS = 10_000
HISTORY_LIMIT = 10_000
TEMP_MIN, TEMP_MAX = -55, 125
SENSOR_COLUMNS = ("t0", "t1")
RANGES = ("Ostatnia godzina", "Dzisiaj", "24 h", "7 dni", "Ostatnie 10 000 pomiarów")


class MissingIndexError(RuntimeError):
    pass


def fetch_json(path, params=None):
    response = requests.get(f"{DB}{path}.json", params=params, timeout=(5, 25))
    # Indeks timestamp pozwala pobierac tylko potrzebne dane.
    if response.status_code == 400:
        try:
            message = str(response.json().get("error", ""))
        except (ValueError, AttributeError):
            message = ""
        if "index" in message.lower():
            raise MissingIndexError(message)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and "error" in data:
        raise ValueError(str(data["error"]))
    return data


def normalize_records(data):
    if not isinstance(data, dict):
        raise ValueError("Nieprawidłowy format historii w bazie.")
    allowed = ("timestamp", "t0", "t1")
    rows = [dict({name: value[name] for name in allowed if name in value}, _key=str(key))
            for key, value in data.items()
            if isinstance(value, dict)]
    df = pd.DataFrame(rows)
    if df.empty or "timestamp" not in df:
        return pd.DataFrame(columns=["_key", "timestamp", "datetime"])
    ts = pd.to_numeric(df["timestamp"], errors="coerce")
    df["datetime"] = pd.to_datetime(ts, unit="ms", utc=True, errors="coerce")
    # Chroni przed np. millis() zamiast Unix ms i uszkodzonymi datami.
    df = df.loc[df["datetime"].notna() & (ts >= 1_577_836_800_000)].copy()
    df["timestamp"] = ts.loc[df.index].astype("int64")
    for sensor in SENSOR_COLUMNS:
        if sensor in df:
            values = pd.to_numeric(df[sensor], errors="coerce")
            df[sensor] = values.where(values.between(TEMP_MIN, TEMP_MAX))
    return df.sort_values(["timestamp", "_key"], kind="stable").reset_index(drop=True)


@st.cache_data(ttl=300, max_entries=8, show_spinner=False)
def load_history(start_ms=None, end_ms=None):
    if start_ms is None:
        return normalize_records(fetch_json(LOGS, {"orderBy": json.dumps("timestamp"),
                                                   "limitToLast": HISTORY_LIMIT}) or {})
    params = {"orderBy": json.dumps("timestamp"), "startAt": start_ms}
    if end_ms is not None:
        params["endAt"] = end_ms
    data = fetch_json(LOGS, params)
    return normalize_records(data or {})


@st.cache_data(ttl=60, max_entries=1, show_spinner=False)
def load_recent():
    data = fetch_json(LOGS, {"orderBy": json.dumps("timestamp"), "limitToLast": 10})
    return normalize_records(data or {})


def load_status():
    try:
        value = fetch_json(STAT)
        return value if isinstance(value, str) else "Brak komunikatu z urządzenia"
    except (requests.RequestException, ValueError, MissingIndexError):
        return "Nie udało się pobrać komunikatu z urządzenia"


def range_start(option, now):
    """now ma strefe UTC; dzis oznacza polnoc w Warszawie, takze przy zmianie czasu."""
    if option in ("Dzisiaj", "Dzisiejszy dzień"):
        return now.tz_convert(TZ).normalize().tz_convert("UTC")
    durations = {"Ostatnia godzina": pd.Timedelta(hours=1),
                 "24 h": pd.Timedelta(hours=24), "7 dni": pd.Timedelta(days=7)}
    return now - durations[option] if option in durations else None


def filter_history(df, start, now):
    mask = df["datetime"] <= now
    if start is not None:
        mask &= df["datetime"] >= start
    return df.loc[mask].copy()


def sensor_columns(df):
    # Dokladna lista: timestamp, receivedAt ani inne metadane nie sa czujnikami.
    return [name for name in SENSOR_COLUMNS if name in df.columns]


def chart_data(df):
    sensors = sensor_columns(df)
    if df.empty or not sensors:
        return pd.DataFrame()
    long = df.melt(id_vars=["timestamp", "datetime"], value_vars=sensors,
                   var_name="sensor", value_name="temp")
    long = long.sort_values(["sensor", "timestamp"], kind="stable").reset_index(drop=True)
    grouped = long.groupby("sensor", sort=False)
    previous_missing = grouped["temp"].shift().isna()
    gap = grouped["timestamp"].diff().gt(GAP_MS)
    # Segmenty tworzymy PRZED usunieciem blednych odczytow.
    long["break"] = long["temp"].isna() | previous_missing | gap
    long["segment"] = long.groupby("sensor", sort=False)["break"].cumsum()
    long = long.dropna(subset=["temp"]).copy()
    long["displayTime"] = long["datetime"].dt.tz_convert(TZ).dt.strftime("%d-%m-%Y %H:%M:%S %Z")
    return long


def reduce_chart_data(long):
    """Dla duzych zakresow zachowaj ekstrema i konce segmentow zamiast calego JSON wykresu."""
    if len(long) <= MAX_CHART_POINTS:
        return long
    data = long.copy()
    low, high = int(data["timestamp"].min()), int(data["timestamp"].max())
    buckets = max(1, MAX_CHART_POINTS // (2 * data["sensor"].nunique()))
    data["bucket"] = ((data["timestamp"] - low) * (buckets - 1) // max(1, high - low)).astype(int)
    groups = data.groupby(["sensor", "segment", "bucket"], sort=False)["temp"]
    indices = set(groups.idxmin()) | set(groups.idxmax())
    for _, segment in data.groupby(["sensor", "segment"], sort=False):
        indices.update([segment.index[0], segment.index[-1]])
    return data.loc[sorted(indices)].drop(columns=["bucket"])


def make_chart(long, start, now):
    low = int(start.timestamp() * 1000) if start is not None else int(long["timestamp"].min())
    high = int(now.timestamp() * 1000) if start is not None else int(long["timestamp"].max())
    if low == high:
        low -= 60_000
        high += 60_000
    ticks = sorted({int(low + (high - low) * i / 7) for i in range(8)})
    labels = {str(t): pd.Timestamp(t, unit="ms", tz="UTC").tz_convert(TZ).strftime("%d-%m %H:%M")
              for t in ticks}
    # Numeryczna os czasu z jawnymi etykietami Warszawy: niezalezna od strefy przegladarki.
    axis = alt.Axis(title="czas", values=ticks, labelAngle=0,
                    labelExpr=f"({json.dumps(labels)})[format(datum.value, '.0f')]")
    base = alt.Chart(long).encode(
        x=alt.X("timestamp:Q", scale=alt.Scale(domain=[low, high], zero=False), axis=axis),
        y=alt.Y("temp:Q", axis=alt.Axis(title="°C")),
        color=alt.Color("sensor:N", title="sensor"),
        tooltip=[alt.Tooltip("sensor:N", title="Czujnik"),
                 alt.Tooltip("temp:Q", format=".2f", title="°C"),
                 alt.Tooltip("displayTime:N", title="Data i godzina")],
    )
    return base.mark_line(point=True).encode(detail="segment:N", order="timestamp:Q").interactive().properties(height=450)


def show_metrics(recent, now):
    sensors = sensor_columns(recent)
    if not sensors:
        st.info("Brak poprawnych odczytów czujników.")
        return
    columns = st.columns(len(sensors))
    for column, sensor in zip(columns, sensors):
        good = recent.loc[recent[sensor].notna()]
        name = f"Czujnik {int(sensor[1:]) + 1}"
        if good.empty:
            column.metric(name, "Brak danych")
            continue
        latest = good.iloc[-1]
        delta = float(latest[sensor] - good.iloc[-2][sensor]) if len(good) > 1 else None
        column.metric(name, f"{latest[sensor]:.2f} °C", None if delta is None else f"{delta:+.2f}")
        age = max(0, int((now - latest["datetime"]).total_seconds() / 60))
        stamp = latest["datetime"].tz_convert(TZ).strftime("%d-%m %H:%M")
        column.caption(f"Ostatni poprawny pomiar: {stamp} · {age} min temu")
        if age > STALE_MINUTES:
            column.warning("Odczyt nieaktualny")
        elif pd.isna(recent.iloc[-1][sensor]):
            column.warning("Brak poprawnego odczytu w ostatniej próbce")


def main():
    st.set_page_config("ESP32 – Historia temperatur", "🌡️", layout="centered")
    st_autorefresh(interval=REFRESH_MS, key="auto")
    st.title("🌡️  Live wykres temperatury")
    option = st.selectbox("Zakres", RANGES, index=2)
    now = pd.Timestamp.now(tz="UTC")
    start = range_start(option, now)
    # Historie odswiezamy co 5 min, 10 najnowszych rekordow co minute.
    # Najnowsze probki dolaczamy do wykresu bez ponownego pobierania calego zakresu.
    query_start = int(start.floor("5min").timestamp() * 1000) if start is not None else None
    query_end = int(now.ceil("5min").timestamp() * 1000)
    try:
        with st.spinner("Pobieranie temperatur…"):
            history = load_history(query_start, query_end)
            recent = filter_history(load_recent(), None, now)
    except MissingIndexError:
        st.error("Dodaj indeks timestamp w regułach Firebase według instrukcji wdrożenia.")
        st.stop()
        return
    except (requests.RequestException, ValueError, MissingIndexError):
        st.error("Nie udało się pobrać historii temperatur. Sprawdź połączenie i dostęp do bazy.")
        st.stop()
        return
    history = (pd.concat([history, recent], ignore_index=True)
               .drop_duplicates("_key", keep="last")
               .sort_values(["timestamp", "_key"], kind="stable"))
    if start is None:
        history = history.tail(HISTORY_LIMIT)
    visible = filter_history(history, start, now)
    long = chart_data(visible)
    if long.empty:
        st.info("Brak pomiarów w wybranym zakresie. Zakres jest liczony od bieżącego czasu.")
    else:
        plot = reduce_chart_data(long)
        # Nie ma bledu Altair przy wiecej niz 5000 wierszy.
        # Ograniczenie wielkosci wykresu jest realizowane jawnie powyzej.
        with alt.data_transformers.disable_max_rows():
            st.altair_chart(make_chart(plot, start, now), use_container_width=True)
        if len(plot) < len(long):
            st.caption("Dla dużego zakresu wykres uproszczono z zachowaniem minimów, maksimów i przerw.")
    # Kafelki sa niezalezne od filtra historii i kolejnosci dosylania rekordow.
    show_metrics(recent, now)
    st.caption(f"🛈 {load_status()}")


if __name__ == "__main__":
    main()
