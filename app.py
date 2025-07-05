import streamlit as st, requests, pandas as pd, altair as alt
from streamlit_autorefresh import st_autorefresh
from datetime import datetime, timedelta

# --- 1. Ustawienia ----------------------------------------------------------
DB_URL = "https://solar-esp-rtdb-default-rtdb.europe-west1.firebasedatabase.app"
PATH   = "/sensor"             # ← zmień na /data_do_grafu gdy będzie historia
REFRESH_MS   = 60_000          # auto-odświeżenie co 1 min
HOURS_TO_KEEP = 48             # trzymamy max 48 h w pamięci sesji

# --- 2. Pobranie rekordu ----------------------------------------------------
def fetch():
    url = f"{DB_URL}{PATH}.json"
    try:
        d = requests.get(url, timeout=10).json() or {}
    except Exception:
        return None
    if "temperature" not in d:
        return None

    ts_raw = d.get("timestamp", 0)
    if ts_raw > 1e11:                          # wygląda na unix-ms
        when = pd.to_datetime(ts_raw, unit="ms", origin="unix")
    else:
        when = pd.Timestamp.now()

    return {"datetime": when, "temperature": float(d["temperature"])}

# --- 3. Konfiguracja strony --------------------------------------------------
st.set_page_config("Live temperatura", "🌡️", layout="centered")
st.title("🌡️  Live wykres temperatury")
st_autorefresh(interval=REFRESH_MS, key="autorefresh")

# --- 4. Historia w session_state -------------------------------------------
hist = st.session_state.get(
    "hist", pd.DataFrame(columns=["temperature","datetime"])
)

row = fetch()
if row:
    row_df = pd.DataFrame([row])

    # ►►  NOWY, „bez-warningowy” sposób dokładania wierszy  ◄◄
    if hist.empty:
        hist = row_df                        # pierwszy wpis
    else:
        hist = pd.concat([hist, row_df], ignore_index=True)

    # przytnij do HOURS_TO_KEEP
    cutoff = pd.Timestamp.now() - pd.Timedelta(hours=HOURS_TO_KEEP)
    hist = hist[hist["datetime"] >= cutoff]

    st.session_state.hist = hist

if hist.empty:
    st.warning("Brak danych w RTDB.")
    st.stop()

# --- 5. Wybór przedziału do wyświetlenia ------------------------------------
option = st.selectbox(
    "Zakres",
    ("Ostatnia godzina", "24 h", "Całość"),
    index=2,
    help="Filtruje dane przed narysowaniem wykresu",
)

now = pd.Timestamp.now()
if option == "Ostatnia godzina":
    df = hist[hist["datetime"] >= now - pd.Timedelta(hours=1)]
elif option == "Dzisiejszy dzień":
    df = hist[hist["datetime"].dt.date == now.date()]
elif option == "24 h":
    df = hist[hist["datetime"] >= now - pd.Timedelta(hours=24)]
else:
    df = hist.copy()

if df.empty:
    st.info("Brak danych w wybranym zakresie.")
    st.stop()

# --- 6. Wykres Altair -------------------------------------------------------
chart = (
    alt.Chart(df)
       .mark_line(point=True)
       .encode(
           x=alt.X("datetime:T", axis=alt.Axis(title="czas", format="%H:%M")),
           y=alt.Y("temperature:Q", axis=alt.Axis(title="°C")),
           tooltip=[
               alt.Tooltip("temperature:Q",
                           title="Temperatura (°C)",
                           format=".2f"),
               alt.Tooltip("datetime:T",
                           title="Czas",
                           format="%d-%m %H:%M")
           ]
       )
       .interactive()
       .properties(height=400)
)
st.altair_chart(chart, use_container_width=True)

# --- 7. Panele metryk -------------------------------------------------------
last = df.iloc[-1]
prev = df.iloc[-2] if len(df) > 1 else last
st.metric(
    "Ostatnia wartość",
    f"{last.temperature:.2f} °C",
    f"{last.temperature - prev.temperature:+.2f} °C",
    help=last.datetime.strftime("%Y-%m-%d %H:%M:%S"),
)
