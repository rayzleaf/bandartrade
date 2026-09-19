"""
Breakout signal bot (day trading) untuk XAU/USD, EUR/USD, dan QQQ (proxy Nasdaq-100).

Logic final (hasil diskusi + riset strategi):
  1. Tandai "session range" (high/low) dari jendela waktu tertentu.
  2. Breakout dikonfirmasi dari CLOSE candle terakhir yang sudah selesai
     (bukan cuma wick sesaat / harga live) -- mengurangi false breakout.
  3. Filter regime volatilitas: breakout cuma valid kalau ATR saat ini
     di atas rata-rata ATR 50 periode terakhir.
  4. Filter momentum: RSI(7) harus BARU SAJA melintasi 50 ke arah breakout
     dalam 1-2 candle terakhir (bukan sekadar "RSI > 50 sekarang").
  5. Khusus QQQ: tambahan konfirmasi volume candle terakhir di atas
     rata-rata 20 candle sebelumnya.
  6. Kalau semua syarat lolos DAN belum ada sinyal open untuk simbol itu,
     catat sinyal baru (entry/SL/TP) ke signals_log.csv dan kirim alert.
  7. Di setiap run, sinyal "open" dicek dua tahap:
       a. Kalau high/low candle terbaru sudah menyentuh TP atau SL,
          sinyal ditutup (exit_type: take_profit/stop_loss).
       b. Kalau belum kena TP/SL tapi jam pantau (monitor_end) simbol
          itu sudah lewat, sinyal DITUTUP PAKSA di harga saat itu
          (exit_type: time_exit). Ini yang membuat sistem konsisten
          sebagai DAY TRADING (exit end-of-day) sesuai strategi yang
          sudah diriset -- tanpa ini, posisi bisa diam-diam menginap
          jadi swing trade yang gak pernah diuji buktinya.

Ini SENGAJA didesain sebagai day trading, bukan scalping atau swing:
  - Scalping (hitungan detik/menit, puluhan+ trade/hari) dihindari karena
    butuh data tick/sub-detik dan eksekusi instan -- gak cocok dengan
    candle 5-15 menit dari tier API gratis dengan jeda beberapa detik.
  - Swing (menginap berhari-hari) BUKAN tujuan desain -- exit end-of-day
    di atas mencegah ini terjadi tanpa sengaja.

Environment variables yang wajib diset:
    TWELVE_DATA_API_KEY   - API key dari twelvedata.com
    TELEGRAM_BOT_TOKEN    - token dari @BotFather
    TELEGRAM_CHAT_ID      - id/username chat/grup/channel tujuan

Environment variable opsional:
    SIGNAL_LOG_PATH       - path file CSV log sinyal (default: signals_log.csv)

Cara jalanin buat testing manual (ganti xxx dengan nilai asli):
    TWELVE_DATA_API_KEY=xxx TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=xxx python breakout_signal_bot.py

Catatan penting (baca sebelum dipakai serius):
  - Jendela waktu QQQ pakai jam pasar AS dalam UTC TETAP (tidak otomatis
    menyesuaikan daylight saving/DST). Perlu direvisi manual ~2x setahun.
  - signals_log.csv ini cuma file LOKAL di direktori tempat script dijalankan.
    Kalau dijalankan via GitHub Actions, file ini HILANG tiap run kecuali
    di-commit balik ke repo di akhir workflow -- lihat scan.yml.
  - Time-exit baru benar-benar tereksekusi kalau script SEMPAT jalan lagi
    setelah monitor_end lewat. Kalau scan.yml berhenti tepat di jam
    monitor_end (bukan sedikit setelahnya), sinyal bisa telat ditutup
    sampai run berikutnya keesokan harinya -- pastikan jadwal cron
    scan.yml sedikit melewati monitor_end tiap simbol (sudah diatur begitu).
  - Rasio risk:reward (R_MULTIPLE) untuk take-profit di-set 1.5x jarak
    stop-loss sebagai default yang masuk akal -- ubah sesuai preferensi.
  - Kalau TP dan SL sama-sama "kesentuh" di candle yang sama (range candle
    lebar), script berasumsi SL kena duluan (asumsi konservatif).
  - Ini fondasi untuk dites manual dulu -- belum melalui backtest resmi
    dengan data historis panjang. Forward-test log ini membantu validasi
    live, tapi bukan pengganti backtest sebelum dipercaya penuh.
"""

import csv
import os
import sys
from datetime import datetime, timezone

import requests

TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

TWELVE_DATA_BASE = "https://api.twelvedata.com"

RSI_PERIOD = 7            # dipendekkan dari 14 -> lebih cepat bereaksi
RSI_LOOKBACK = 3          # butuh nilai sekarang + 2 candle sebelumnya
RSI_LEVEL = 50

ATR_PERIOD = 14
ATR_BASELINE_LOOKBACK = 50  # jumlah pembacaan ATR buat hitung rata-rata baseline

VOLUME_LOOKBACK = 20      # khusus simbol yang punya use_volume_confirmation

R_MULTIPLE = 1.5          # take-profit = entry +/- R_MULTIPLE x jarak stop-loss

# Jarak stop-loss dihitung dari ATR saat ini, BUKAN dari lebar session range.
# Alasannya: range EUR/USD & XAU/USD sekarang mencakup 6 jam (sesi London),
# yang bisa sangat lebar di hari volatil -- kalau stop-loss dipatok ke sisi
# berlawanan range itu, jaraknya bisa gak proporsional dengan sisa waktu
# trading (cuma 4 jam sebelum time-exit). ATR menyesuaikan otomatis ke
# kondisi volatilitas terkini, jadi target take-profit tetap realistis
# dicapai dalam jendela waktu yang ada. Range sesi tetap dipakai buat
# mendeteksi LEVEL breakout-nya -- cuma lebar stop-nya yang diganti.
STOP_ATR_MULTIPLE = 1.5

# Breakout cuma dianggap valid kalau close menembus range LEBIH DARI buffer ini
# (persentase dari ATR saat ini) -- bukan cuma menembus sedikit sekali. Ini
# langsung merespons temuan riset kita: breakout tipis itu gampang habis
# dimakan spread/slippage sebelum sempat profit (SQN/profit factor breakout
# ORB anjlok ke ~break-even begitu spread realistis dihitung). Menskalakan
# ke ATR (bukan angka pip/dolar tetap) supaya adil buat 3 instrumen dengan
# skala harga berbeda jauh (EUR/USD ~1, XAU/USD ~4000, QQQ ~700).
BREAKOUT_BUFFER_ATR_FRACTION = 0.10

LOG_PATH = os.environ.get("SIGNAL_LOG_PATH", "signals_log.csv")
LOG_FIELDS = [
    "signal_id", "symbol", "direction", "entry_price", "stop_loss",
    "take_profit", "entry_time", "status", "close_price", "close_time",
    "exit_type", "result", "return_pct",
]

# Semua waktu dalam UTC, format "HH:MM".
# "range_start/end"   -> jendela buat menandai high/low sesi acuan
# "monitor_start/end" -> jendela mencari breakout DAN batas exit end-of-day
# "use_volume"        -> True kalau breakout perlu konfirmasi volume di atas rata-rata
SYMBOLS = {
    # Range ditandai dari sesi London (habis London open, sebelum NY buka),
    # breakout dipantau saat overlap London-NY (13:00-17:00 UTC) -- window
    # dengan volatilitas dan volume tertinggi untuk kedua instrumen ini
    # menurut riset sesi trading, bukan cuma "London open" seperti versi
    # sebelumnya.
    "EUR/USD": {
        # DI-PAUSE (enabled=False) per hasil backtest: 12 trade, profit
        # factor 0.32, dominan kalah lewat stop_loss (whipsaw setelah
        # breakout, bukan momentum lanjut). Dua hipotesis perbaikan sudah
        # diuji dan SAMA-SAMA GAGAL memperbaiki (skip jam rilis berita:
        # PF 0.35; stop dilebarkan 2x ATR: PF 0.34) -- jadi bukan sekadar
        # salah parameter. Config tetap disimpan (bukan dihapus) supaya
        # gampang diaktifkan lagi begitu ada cukup data forward-test baru
        # buat evaluasi ulang. Sinyal yang kebetulan sudah open tetap
        # dilacak sampai closed walau enabled=False (lihat main()).
        "interval": "15min",
        "range_start": "07:00",
        "range_end": "13:00",
        "monitor_start": "13:00",
        "monitor_end": "17:00",
        "use_volume": False,
        "enabled": False,
    },
    "XAU/USD": {
        "interval": "15min",
        "range_start": "07:00",
        "range_end": "13:00",
        "monitor_start": "13:00",
        "monitor_end": "17:00",
        "use_volume": False,
        "enabled": True,
    },
    "QQQ": {
        # 13:30-13:45 UTC = 9:30-9:45 pagi waktu New York (EDT, musim panas)
        "interval": "5min",
        "range_start": "13:30",
        "range_end": "13:45",
        "monitor_start": "13:45",
        "monitor_end": "16:00",
        "use_volume": True,
        "enabled": True,
        "broker_note": (
            "QQQ dipakai sebagai proxy Nasdaq-100 karena datanya gratis. "
            "Broker kamu kemungkinan pakai simbol berbeda (US100/NAS100/USTECH). "
            "Pakai arah sinyalnya, JANGAN pakai level harga entry/SL/TP di atas "
            "secara mentah -- skala harga QQQ dan US100/NAS100 di broker beda."
        ),
    },
}


def _time_in_range(t, start, end):
    return start <= t <= end


# ---------------------------------------------------------------------------
# Data candle & indikator (Twelve Data)
# ---------------------------------------------------------------------------

def get_today_candles(symbol, interval, outputsize=100):
    resp = requests.get(
        f"{TWELVE_DATA_BASE}/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC",
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=15,
    )
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error (time_series) untuk {symbol}: {data}")
    return data["values"]  # urutan terbaru dulu


def get_session_range(candles, cfg):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    highs, lows = [], []
    for c in candles:
        c_date, c_time = c["datetime"].split(" ")
        if c_date != today:
            continue
        if _time_in_range(c_time[:5], cfg["range_start"], cfg["range_end"]):
            highs.append(float(c["high"]))
            lows.append(float(c["low"]))
    if not highs:
        return None, None
    return max(highs), min(lows)


def get_latest_closed_candle(candles):
    return candles[0]


def get_atr_series(symbol, interval, period=ATR_PERIOD, outputsize=ATR_BASELINE_LOOKBACK):
    resp = requests.get(
        f"{TWELVE_DATA_BASE}/atr",
        params={
            "symbol": symbol,
            "interval": interval,
            "time_period": period,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=15,
    )
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error (atr) untuk {symbol}: {data}")
    return [float(v["atr"]) for v in data["values"]]


def atr_regime_ok(atr_values):
    if len(atr_values) < 2:
        return False
    current_atr = atr_values[0]
    baseline_atr = sum(atr_values) / len(atr_values)
    return current_atr > baseline_atr


def get_rsi_series(symbol, interval, period=RSI_PERIOD, outputsize=RSI_LOOKBACK):
    resp = requests.get(
        f"{TWELVE_DATA_BASE}/rsi",
        params={
            "symbol": symbol,
            "interval": interval,
            "time_period": period,
            "outputsize": outputsize,
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=15,
    )
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error (rsi) untuk {symbol}: {data}")
    return [float(v["rsi"]) for v in data["values"]]


def rsi_recently_crossed(rsi_values, direction):
    if len(rsi_values) < 2:
        return False
    current = rsi_values[0]
    recent_history = rsi_values[1:3]
    if direction == "up":
        if current <= RSI_LEVEL:
            return False
        return any(v <= RSI_LEVEL for v in recent_history)
    else:
        if current >= RSI_LEVEL:
            return False
        return any(v >= RSI_LEVEL for v in recent_history)


def volume_confirmed(candles, lookback=VOLUME_LOOKBACK):
    latest_volume = candles[0].get("volume")
    if latest_volume is None:
        return True
    history = [
        float(c["volume"])
        for c in candles[1 : lookback + 1]
        if c.get("volume") is not None
    ]
    if not history:
        return True
    avg_volume = sum(history) / len(history)
    return float(latest_volume) > avg_volume


# ---------------------------------------------------------------------------
# Log sinyal (forward-test tracking)
# ---------------------------------------------------------------------------

def load_signal_log():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_signal_log(rows):
    with open(LOG_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def has_open_signal(rows, symbol):
    return any(r["symbol"] == symbol and r["status"] == "open" for r in rows)


def append_new_signal(rows, symbol, direction, entry_price, stop_loss, take_profit, entry_time_str):
    signal_id = f"{symbol.replace('/', '')}-{entry_time_str.replace(' ', 'T').replace(':', '')}"
    rows.append({
        "signal_id": signal_id,
        "symbol": symbol,
        "direction": direction,
        "entry_price": f"{entry_price:.5f}",
        "stop_loss": f"{stop_loss:.5f}",
        "take_profit": f"{take_profit:.5f}",
        "entry_time": entry_time_str,
        "status": "open",
        "close_price": "",
        "close_time": "",
        "exit_type": "",
        "result": "",
        "return_pct": "",
    })
    return signal_id


def _close_row(row, close_price, exit_type, now_utc):
    entry_price = float(row["entry_price"])
    direction = row["direction"]
    return_pct = (
        (close_price - entry_price) / entry_price * 100
        if direction == "up"
        else (entry_price - close_price) / entry_price * 100
    )
    if return_pct > 0:
        result = "win"
    elif return_pct < 0:
        result = "loss"
    else:
        result = "breakeven"

    row["status"] = "closed"
    row["close_price"] = f"{close_price:.5f}"
    row["close_time"] = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    row["exit_type"] = exit_type
    row["result"] = result
    row["return_pct"] = f"{return_pct:.2f}"


def check_tp_sl_closures(rows, candles_by_symbol, now_utc):
    """Cek tiap sinyal 'open': kalau high/low candle terbaru simbolnya
    sudah menyentuh TP atau SL, tutup sinyal itu dan catat hasilnya."""
    closed_this_run = []
    for row in rows:
        if row["status"] != "open":
            continue
        candles = candles_by_symbol.get(row["symbol"])
        if not candles:
            continue

        latest = candles[0]
        high, low = float(latest["high"]), float(latest["low"])
        direction = row["direction"]
        take_profit = float(row["take_profit"])
        stop_loss = float(row["stop_loss"])

        hit_tp = (high >= take_profit) if direction == "up" else (low <= take_profit)
        hit_sl = (low <= stop_loss) if direction == "up" else (high >= stop_loss)

        if not (hit_tp or hit_sl):
            continue

        # Asumsi konservatif kalau dua-duanya kesentuh di candle yang sama.
        if hit_sl:
            _close_row(row, stop_loss, "stop_loss", now_utc)
        else:
            _close_row(row, take_profit, "take_profit", now_utc)
        closed_this_run.append(row)

    return closed_this_run


def check_time_exits(rows, candles_by_symbol, now_utc):
    """Tutup paksa sinyal yang masih open begitu jam pantau (monitor_end)
    simbolnya sudah lewat -- ini yang membuat sistem konsisten sebagai
    DAY TRADING (exit end-of-day), sesuai strategi yang sudah diriset.
    Tanpa ini, posisi yang gak kena TP/SL bisa diam-diam menginap dan
    berubah jadi swing trade yang gak pernah diuji buktinya."""
    now_time = now_utc.strftime("%H:%M")
    closed_this_run = []

    for row in rows:
        if row["status"] != "open":
            continue
        cfg = SYMBOLS.get(row["symbol"])
        if cfg is None or now_time < cfg["monitor_end"]:
            continue  # jam pantau belum lewat, biarkan tetap open

        candles = candles_by_symbol.get(row["symbol"])
        if not candles:
            continue  # gak ada data buat nutup, coba lagi run berikutnya

        close_price = float(candles[0]["close"])
        _close_row(row, close_price, "time_exit", now_utc)
        closed_this_run.append(row)

    return closed_this_run


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_alert(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15
    )
    if not resp.ok:
        print(f"[!] Gagal kirim ke Telegram: {resp.text}")


# ---------------------------------------------------------------------------
# Evaluasi per simbol (cari sinyal baru)
# ---------------------------------------------------------------------------

def evaluate_symbol(symbol, cfg, candles, log_rows):
    now_utc = datetime.now(timezone.utc)
    now_time = now_utc.strftime("%H:%M")

    if not _time_in_range(now_time, cfg["monitor_start"], cfg["monitor_end"]):
        print(f"[{symbol}] Di luar jam pantau ({cfg['monitor_start']}-{cfg['monitor_end']} UTC), skip.")
        return

    if has_open_signal(log_rows, symbol):
        print(f"[{symbol}] Masih ada sinyal open, skip cari sinyal baru.")
        return

    session_high, session_low = get_session_range(candles, cfg)
    if session_high is None:
        print(f"[{symbol}] Belum ada data range sesi hari ini, skip.")
        return

    latest_candle = get_latest_closed_candle(candles)
    close_price = float(latest_candle["close"])

    atr_values = get_atr_series(symbol, cfg["interval"])
    if len(atr_values) < 2:
        print(f"[{symbol}] Data ATR belum cukup, skip.")
        return

    breakout_buffer = BREAKOUT_BUFFER_ATR_FRACTION * atr_values[0]

    candidate_direction = None
    if close_price > session_high + breakout_buffer:
        candidate_direction = "up"
    elif close_price < session_low - breakout_buffer:
        candidate_direction = "down"

    if candidate_direction is None:
        print(
            f"[{symbol}] Belum breakout (dengan buffer {breakout_buffer:.5f}). "
            f"Close={close_price}, range={session_low}-{session_high}"
        )
        return

    if not atr_regime_ok(atr_values):
        print(f"[{symbol}] Breakout candidate ({candidate_direction}) tapi ATR di bawah baseline, skip.")
        return

    rsi_values = get_rsi_series(symbol, cfg["interval"])
    if not rsi_recently_crossed(rsi_values, candidate_direction):
        print(
            f"[{symbol}] Breakout candidate ({candidate_direction}) tapi RSI belum "
            f"cross {RSI_LEVEL} baru-baru ini (RSI sekarang={rsi_values[0]:.1f}), skip."
        )
        return

    if cfg.get("use_volume") and not volume_confirmed(candles):
        print(f"[{symbol}] Breakout candidate ({candidate_direction}) tapi volume di bawah rata-rata, skip.")
        return

    stop_distance = STOP_ATR_MULTIPLE * atr_values[0]
    if candidate_direction == "up":
        stop_loss = close_price - stop_distance
        take_profit = close_price + R_MULTIPLE * stop_distance
    else:
        stop_loss = close_price + stop_distance
        take_profit = close_price - R_MULTIPLE * stop_distance

    signal_id = append_new_signal(
        log_rows, symbol, candidate_direction, close_price, stop_loss, take_profit,
        latest_candle["datetime"],
    )

    direction_label = "BUY (breakout atas)" if candidate_direction == "up" else "SELL (breakout bawah)"
    message = (
        f"Sinyal baru: {signal_id}\n"
        f"{symbol} -- {direction_label}\n"
        f"Entry: {close_price:.5f}\n"
        f"Stop Loss: {stop_loss:.5f}\n"
        f"Take Profit: {take_profit:.5f} (R:R {R_MULTIPLE})\n"
        f"RSI({RSI_PERIOD}): {rsi_values[0]:.1f} | ATR({ATR_PERIOD}): {atr_values[0]:.5f}\n"
        f"Exit paksa (kalau belum kena TP/SL) jam: {cfg['monitor_end']} UTC\n"
        f"Waktu: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    if cfg.get("broker_note"):
        message += f"\n\nCatatan: {cfg['broker_note']}"
    print(message)
    send_telegram_alert(message)


def main():
    required = {
        "TWELVE_DATA_API_KEY": TWELVE_DATA_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"[!] Environment variable belum diset: {', '.join(missing)}")
        sys.exit(1)

    now_utc = datetime.now(timezone.utc)
    log_rows = load_signal_log()

    candles_by_symbol = {}
    for symbol, cfg in SYMBOLS.items():
        try:
            candles_by_symbol[symbol] = get_today_candles(symbol, cfg["interval"])
        except Exception as e:
            print(f"[!] Gagal ambil candle {symbol}: {e}")
            candles_by_symbol[symbol] = None

    valid_candles = {s: c for s, c in candles_by_symbol.items() if c is not None}

    # 1. Cek dulu sinyal yang masih open lewat TP/SL.
    closed_tp_sl = check_tp_sl_closures(log_rows, valid_candles, now_utc)

    # 2. Untuk yang masih open setelah itu, cek apakah waktunya exit paksa
    #    (day trading -- jangan biarkan menginap jadi swing tanpa sengaja).
    closed_time = check_time_exits(log_rows, valid_candles, now_utc)

    for row in closed_tp_sl + closed_time:
        send_telegram_alert(
            f"Sinyal ditutup: {row['signal_id']} ({row['exit_type']})\n"
            f"Hasil: {row['result'].upper()}\n"
            f"Entry: {row['entry_price']} -> Close: {row['close_price']}\n"
            f"Return: {row['return_pct']}%"
        )

    # 3. Cari sinyal baru untuk simbol yang enabled DAN candle-nya berhasil diambil.
    #    Simbol enabled=False (mis. EUR/USD saat ini) tetap diproses di langkah 1-2
    #    di atas kalau kebetulan masih ada sinyal open miliknya -- yang di-skip di
    #    sini cuma pencarian sinyal BARU.
    for symbol, cfg in SYMBOLS.items():
        if not cfg.get("enabled", True):
            print(f"[{symbol}] Di-pause (enabled=False), skip cari sinyal baru.")
            continue
        candles = candles_by_symbol.get(symbol)
        if candles is None:
            continue
        try:
            evaluate_symbol(symbol, cfg, candles, log_rows)
        except Exception as e:
            print(f"[!] Error evaluasi {symbol}: {e}")

    save_signal_log(log_rows)

    closed_all = [r for r in log_rows if r["status"] == "closed"]
    if closed_all:
        wins = [r for r in closed_all if r["result"] == "win"]
        time_exits = [r for r in closed_all if r["exit_type"] == "time_exit"]
        print(
            f"[Ringkasan] Total closed: {len(closed_all)} "
            f"(time_exit: {len(time_exits)}), win rate: {len(wins) / len(closed_all) * 100:.1f}%"
        )


if __name__ == "__main__":
    main()
