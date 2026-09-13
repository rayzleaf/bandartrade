"""
Dashboard Streamlit untuk memantau signals_log.csv dari breakout_signal_bot.

Cara jalanin LOKAL (buat cek dulu sebelum deploy):
    pip install streamlit pandas
    streamlit run dashboard.py

Cara deploy GRATIS (biar bisa diakses dari HP/browser mana saja, gak perlu
PC nyala -- persis semangat yang sama dengan bot-nya sendiri):
    1. Push file ini ke repo GitHub yang SAMA dengan breakout_signal_bot.py
       (repo yang sudah kamu buat sebelumnya).
    2. Buka share.streamlit.io, login pakai akun GitHub.
    3. Klik "New app", pilih repo ini, branch main, file path "dashboard.py".
    4. Deploy. Streamlit Cloud otomatis re-run setiap kali signals_log.csv
       di repo berubah (karena GitHub Actions commit update tiap ~15 menit),
       jadi dashboard ini akan selalu menampilkan data terbaru tanpa kamu
       perlu redeploy manual.

Catatan: profit factor & equity curve di sini dihitung dari return_pct
per sinyal (bobot sama rata tiap sinyal) -- BUKAN disesuaikan dengan
besar posisi/lot yang benar-benar kamu pakai kalau nanti eksekusi manual.
Anggap ini indikator arah, bukan P&L riil akun kamu.
"""

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Signal Bot Dashboard", layout="wide")
st.title("📊 Breakout Signal Bot — Forward Test Dashboard")

LOG_PATH = "signals_log.csv"

try:
    df = pd.read_csv(LOG_PATH)
except FileNotFoundError:
    st.warning("signals_log.csv belum ditemukan. Jalankan bot minimal sekali dulu.")
    st.stop()

if df.empty:
    st.info("File sudah ada tapi belum ada sinyal tercatat.")
    st.stop()

df["return_pct"] = pd.to_numeric(df["return_pct"], errors="coerce")

closed = df[df["status"] == "closed"].copy()
open_signals = df[df["status"] == "open"].copy()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total sinyal", len(df))
col2.metric("Sinyal masih open", len(open_signals))

if not closed.empty:
    win_rate = (closed["result"] == "win").mean() * 100
    wins_sum = closed.loc[closed["return_pct"] > 0, "return_pct"].sum()
    losses_sum = -closed.loc[closed["return_pct"] < 0, "return_pct"].sum()
    profit_factor = (wins_sum / losses_sum) if losses_sum > 0 else float("inf")
    avg_return = closed["return_pct"].mean()

    col3.metric("Win rate", f"{win_rate:.1f}%")
    col4.metric("Profit factor", f"{profit_factor:.2f}")

    st.caption(
        "Profit factor & rata-rata return dihitung per sinyal (bobot sama "
        "rata), bukan P&L riil akun -- lihat catatan di kode."
    )

    st.subheader("Equity curve (kumulatif return %, bobot sama rata per sinyal)")
    closed_sorted = closed.sort_values("close_time")
    closed_sorted["cumulative_return"] = closed_sorted["return_pct"].cumsum()
    st.line_chart(closed_sorted.set_index("close_time")["cumulative_return"])

    st.subheader("Breakdown per simbol")
    per_symbol = closed.groupby("symbol").agg(
        total=("result", "count"),
        win_rate_pct=("result", lambda x: round((x == "win").mean() * 100, 1)),
        avg_return_pct=("return_pct", lambda x: round(x.mean(), 2)),
    )
    st.dataframe(per_symbol, use_container_width=True)

    st.subheader("Breakdown per jenis exit (take_profit / stop_loss / time_exit)")
    per_exit = closed.groupby("exit_type").agg(
        total=("result", "count"),
        win_rate_pct=("result", lambda x: round((x == "win").mean() * 100, 1)),
    )
    st.dataframe(per_exit, use_container_width=True)
else:
    st.info("Belum ada sinyal yang closed -- semuanya masih open.")

st.subheader("Sinyal yang sedang open")
if open_signals.empty:
    st.write("Tidak ada sinyal open saat ini.")
else:
    st.dataframe(open_signals, use_container_width=True)

st.subheader("Riwayat lengkap (terbaru dulu)")
st.dataframe(df.sort_values("entry_time", ascending=False), use_container_width=True)
