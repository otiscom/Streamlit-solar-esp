import streamlit as st, requests, pandas as pd, altair as alt
from streamlit_autorefresh import st_autorefresh

# ────────── konfiguracja  ────────────────────────────────────────
DB   = "https://solar-esp-rtdb-default-rtdb.europe-west1.firebasedatabase.app"
LOGS = "/logs"
STAT = "/log"

REFRESH_MS  = 60_000
MAX_RECORDS = 2_880      # 48 h przy próbce / min
TEMP_MIN, TEMP_MAX = -50, 100

# ────────── pobieranie historii ─────────────────────────────────
@st.cache_data(ttl=60)
def load_history():
    url  = f"{DB}{LOGS}.json?orderBy=\"$key\"&limitToLast={MAX_RECORDS}"
    data = requests.get(url, timeout=10).json() or {}
    rows = [v | {"_k": k} for k, v in sorted(data.items())]
    df   = pd.DataFrame(rows)

    if not df.empty and "timestamp" in df:
        ts = pd.to_numeric(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])
        # → czas Warszawy, potem „naive” (bez strefy) do Pandas/Altair
        dt = (
            pd.to_datetime(ts, unit="ms", origin="unix", utc=True)
              .dt.tz_localize(None)
        )
        df["datetime"] = dt
    return df

def load_status():
    try:
        return requests.get(f"{DB}{STAT}.json", timeout=5).json()
    except Exception:
        return "Brak komunikatu"

# ────────── UI podstawowe  ───────────────────────────────────────
st.set_page_config("ESP32 – Historia temperatur", "🌡️", layout="centered")
st_autorefresh(interval=REFRESH_MS, key="auto")

st.title("🌡️  Live wykres temperatury (3 czujniki)")

df     = load_history()
status = load_status()

if df.empty or "datetime" not in df:
    st.warning("Brak danych w RTDB.")
    st.stop()

# ────────── wybór zakresu  ───────────────────────────────────────
opt = st.selectbox(
    "Zakres",
    ("Ostatnia godzina", "Dzisiejszy dzień", "24 h", "7 dni", "Całość"),
    index=2
)

now = pd.Timestamp.utcnow()
start = None                             # granica lewa do osi X

if opt == "Ostatnia godzina":
    start = now - pd.Timedelta(hours=1)
elif opt == "Dzisiejszy dzień":
    start = now.normalize()              # dziś 00:00
elif opt == "24 h":
    start = now - pd.Timedelta(hours=24)
elif opt == "7 dni":
    start = now - pd.Timedelta(days=7)

# filtr danych (jeżeli start zdefiniowany)
if start is not None:
    df = df[(df["datetime"] >= start) & (df["datetime"] <= now)]

# ────────── czyszczenie błędnych temperatur  ─────────────────────
value_cols = [c for c in df.columns if c.startswith("t")]
for col in value_cols:
    df[col] = pd.to_numeric(df[col], errors="coerce")
    df.loc[(df[col] < TEMP_MIN) | (df[col] > TEMP_MAX), col] = None

# ────────── long format  ─────────────────────────────────────────
long = (
    df.melt(id_vars=["datetime"],
            value_vars=value_cols,
            var_name="sensor",
            value_name="temp")
      .dropna(subset=["temp"])
)

if long.empty:
    st.info("Brak danych w wybranym zakresie.")
    st.stop()

# ────────── wykres Altair  ───────────────────────────────────────
x_scale = alt.Scale(domain=[start, now]) if start is not None else alt.Undefined

chart = (
    alt.Chart(long)
       .mark_line(point=True)
       .encode(
           x=alt.X("datetime:T",
                   scale=x_scale,
                   axis=alt.Axis(title="czas", format="%d-%m %H:%M")),
           y=alt.Y("temp:Q", axis=alt.Axis(title="°C")),
           color="sensor:N",
           tooltip=["sensor",
                    alt.Tooltip("temp:Q", format=".2f", title="°C"),
                    alt.Tooltip("datetime:T", title="czas")]
       )
       .interactive()
       .properties(height=450)
)
st.altair_chart(chart, use_container_width=True)

# ────────── metryki  ─────────────────────────────────────────────
cols = st.columns(len(value_cols))
for i, col in enumerate(value_cols):
    series = df[col].dropna()
    if series.empty:
        cols[i].metric(f"Czujnik {i}", "Brak danych")
    else:
        latest = series.iloc[-1]
        delta  = latest - series.iloc[-2] if len(series) > 1 else 0
        cols[i].metric(f"Czujnik {i}", f"{latest:.2f} °C", f"{delta:+.2f}")

# ────────── status / log  ────────────────────────────────────────
st.caption(f"🛈 {status}")
