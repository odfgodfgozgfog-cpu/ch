"""
WEATHER BOT v4.1 — ANALYSIS-BASED OPTIMIZATION
(BTC Up/Down 5m — отдельный проект: ../btc_updown_bot/, не смешивать с этим файлом.)

Changes from v3.9 (after full PnL analysis of 42 positions):
1. ✅ STRICT LADDER ±1°C: только 3 температуры (GFS-1, GFS, GFS+1)
2. ✅ MIN PRICE $0.005: не режем penny trades (Toronto/Ankara profitable)
3. ✅ BLOCKED: +Shanghai, +Beijing (0% WinRate)
4. ✅ PRICE RANGE: $0.005-$0.05 (NeoBrother sweet spot)
8. ✅ BLOCKED CITIES: Munich, Singapore, Tokyo, HK
9. ✅ REAL GFS API + True Edge Model
10. ✅ v4.1: гибкая лестница — хвосты только с якорем по пику; 1×YES якорь при ANCHOR_*; не all-or-nothing
"""

import os
import atexit
import asyncio
import csv
import json
import re
import time
from decimal import Decimal, ROUND_HALF_UP
import math
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor


def _ensure_numpy_pandas() -> None:
    """
    Ставит numpy/pandas в тот же интерпретатор, что запускает скрипт (критично для .venv на Windows).
    Без этого `py weather_bot_v3_qwen.py` часто падает с ModuleNotFoundError, если pip ставили в другой Python.
    """
    import sys

    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        return
    except ImportError:
        pass
    print(
        "weather_bot: нет numpy/pandas — выполняю: "
        f'"{sys.executable}" -m pip install numpy pandas',
        flush=True,
    )
    subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "pandas"])


_ensure_numpy_pandas()
import numpy as np
import pandas as pd
import requests
import urllib3
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Отключаем SSL warnings (проблема Windows)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("weather_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Отключаем спам HTTP запросов от py_clob_client и requests
logging.getLogger("py_clob_client").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Фикс Unicode для Windows консоли
import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

# Не перетираем явные переменные окружения из запуска.
# Это критично для безопасных smoke-тестов с принудительным DRY_RUN=true.
load_dotenv(override=False, dotenv_path=os.path.join(os.path.dirname(__file__), '.env'))


def _position_end_date_iso(pos: dict) -> str | None:
    """Дата резолва позиции YYYY-MM-DD из Polymarket endDate (или None)."""
    end_str = (pos.get("endDate") or "").strip()
    if not end_str:
        return None
    try:
        if "T" in end_str:
            end_date = datetime.fromisoformat(end_str.split(".")[0].replace("Z", ""))
        else:
            end_date = datetime.strptime(end_str.split(" ")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return end_date.strftime("%Y-%m-%d")
    except Exception:
        return None


def _position_end_datetime_utc(pos: dict) -> datetime | None:
    """Парсит endDate позиции в UTC (как в positions_losers_report / get_active_positions)."""
    end_str = (pos.get("endDate") or "").strip()
    if not end_str:
        return None
    try:
        clean = end_str.split(".")[0].replace("Z", "")
        if "T" in clean:
            end_date = datetime.fromisoformat(clean)
        else:
            end_date = datetime.strptime(clean.split(" ")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        return end_date
    except Exception:
        return None


def is_weather_market_title(title: str | None) -> bool:
    """Тот же фильтр, что в positions_losers_report (погодные маркеты по заголовку)."""
    t = (title or "").lower()
    return "temperature" in t or ("highest" in t and "°" in (title or ""))


def _position_api_mark_usd(pos: dict) -> float:
    """Оценка mark в $ по снимку Data API (currentValue или size×curPrice)."""
    try:
        v = pos.get("currentValue")
        if v is not None and v != "":
            return float(v)
    except (TypeError, ValueError):
        pass
    try:
        return float(pos.get("size") or 0) * float(pos.get("curPrice") or 0)
    except (TypeError, ValueError):
        return 0.0


def _title_strike_temps_c(title_lower: str) -> list[int]:
    """Все числа перед °C/°c в заголовке (например -2 и 12), без путаницы -2 vs +2."""
    return [int(m.group(1)) for m in re.finditer(r"(-?\d+)\s*°c", title_lower)]


def is_bucket_encoded_temp(temp) -> bool:
    try:
        return int(temp) >= 100_000
    except (TypeError, ValueError):
        return False


def format_purchase_temp_label(temp) -> str:
    """Человекочитаемая метка для purchase_key / логов (exact °C и encoded bucket)."""
    try:
        t = int(temp)
    except (TypeError, ValueError):
        return str(temp)
    if t >= 400_000:
        return f"≤{t - 400_000}°F"
    if t >= 300_000:
        return f"≥{t - 300_000}°F"
    if t >= 200_000:
        return f"≤{t - 200_000}°C bucket"
    if t >= 100_000:
        return f"≥{t - 100_000}°C bucket"
    return f"{t}°C"


# ==========================================
# ⚙️  КОНФИГУРАЦИЯ
# ==========================================

# API & Wallet
POLY_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLY_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLY_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
PRIVATE_KEY = os.getenv("POLYMARKET_PK")
FUNDER_ADDRESS = os.getenv("WALLET_ADDRESS", "0x42Dc61b9e7dF40dbF05b10D0E85b4019C977bC41")

# Стратегия
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "false").lower() in ("true", "1", "yes", "live")
if not DRY_RUN and not ALLOW_LIVE_TRADING:
    logger.warning("   🛡️ Live trading lock active: ALLOW_LIVE_TRADING!=true, принудительно включаем DRY_RUN")
    DRY_RUN = True

# Режим стратегии: legacy | ai_weather (см. WEATHER_AI_STRATEGY_PLAN.md)
# По умолчанию ai_weather — выходы/бакеты как в плане выравнивания под automatedAITradingbot
STRATEGY_MODE = os.getenv("STRATEGY_MODE", "ai_weather").strip().lower()
AI_WEATHER = STRATEGY_MODE in ("ai_weather", "ai", "automatedai")

# Баланс «объём сигналов ↔ качество» (дефолты ai_weather): 0 = мягче/больше входов, 1 = строже. off = без блендинга.
# Явные MIN_EDGE, CONFIDENCE_*, MAX_ENSEMBLE_SPREAD_C в .env перекрывают соответствующий дефолт.
_eqb_raw = os.getenv("ENTRY_QUALITY_BALANCE", "").strip()
if not _eqb_raw:
    ENTRY_QUALITY_BALANCE = 0.45 if AI_WEATHER else None
elif _eqb_raw.lower() in ("off", "manual", "none"):
    ENTRY_QUALITY_BALANCE = None
else:
    try:
        ENTRY_QUALITY_BALANCE = max(0.0, min(1.0, float(_eqb_raw)))
    except ValueError:
        ENTRY_QUALITY_BALANCE = 0.45 if AI_WEATHER else None


def _entry_qb(lo, hi, t):
    return lo + (hi - lo) * t


def _parse_csv_floats(raw, default):
    if raw is None or str(raw).strip() == "":
        return list(default)
    out = []
    for p in str(raw).split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(float(p))
        except ValueError:
            continue
    return out if out else list(default)


if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None:
    _t = ENTRY_QUALITY_BALANCE
    _def_min_edge = _entry_qb(0.050, 0.066, _t)
    _def_max_spread = _entry_qb(3.0, 2.0, _t)
    _def_min_models = 3 if _t >= 0.62 else 2
else:
    _def_min_edge = 0.058 if AI_WEATHER else 0.03
    _def_max_spread = 2.5 if AI_WEATHER else 0.0
    _def_min_models = 2 if AI_WEATHER else 1

MIN_EDGE = float(os.getenv("MIN_EDGE", str(_def_min_edge)))
MIN_GFS_CONFIDENCE = 0.50
# Качество входа: не торговать при сильном расхождении моделей (0 = выкл.)
_mesp = os.getenv("MAX_ENSEMBLE_SPREAD_C", "").strip()
MAX_ENSEMBLE_SPREAD_C = float(_mesp) if _mesp else float(_def_max_spread)
_mmc = os.getenv("MIN_MODELS_FOR_ENTRY", "").strip()
MIN_MODELS_FOR_ENTRY = max(1, int(_mmc)) if _mmc else int(_def_min_models)
POLYMARKET_COMMISSION = 0.02  # 2% комиссия Polymarket

# Этап A: при очень дешёвой цене проверяем абсолютный зазор model_prob - effective_price
# В ai_weather пороги выше: 2% комиссия + спред съедают «edge» 1–2%
EDGE_ABSOLUTE_PRICE_THRESHOLD = float(
    os.getenv("EDGE_ABSOLUTE_PRICE_THRESHOLD", "0.03" if AI_WEATHER else "0.02")
)
_eam_def = (
    _entry_qb(0.038, 0.054, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.048 if AI_WEATHER else 0.01)
)
EDGE_ABSOLUTE_MIN = float(os.getenv("EDGE_ABSOLUTE_MIN_WHEN_PRICE_BELOW_0.02", str(_eam_def)))

# Тайминги (часы): в ai_weather — окно 24–72 ч по умолчанию (план automatedAI)
if AI_WEATHER:
    MIN_HOURS_TO_EXPIRY = int(os.getenv("BUY_HOURS_BEFORE_MIN_AI", os.getenv("BUY_HOURS_BEFORE_MIN", "24")))
    MAX_HOURS_TO_EXPIRY = int(os.getenv("BUY_HOURS_BEFORE_MAX_AI", os.getenv("BUY_HOURS_BEFORE_MAX", "72")))
else:
    MIN_HOURS_TO_EXPIRY = int(os.getenv("BUY_HOURS_BEFORE_MIN", "6"))
    MAX_HOURS_TO_EXPIRY = int(os.getenv("BUY_HOURS_BEFORE_MAX", "24"))

# Максимальная цена контракта.
if AI_WEATHER:
    MIN_CONTRACT_PRICE = float(os.getenv("AI_WEATHER_MIN_PRICE", "0.005"))
    MAX_CONTRACT_PRICE = float(os.getenv("AI_WEATHER_MAX_PRICE", os.getenv("MAX_CONTRACT_PRICE", "0.05")))
else:
    MIN_CONTRACT_PRICE = 0.005
    MAX_CONTRACT_PRICE = float(os.getenv("MAX_CONTRACT_PRICE", 0.05))
CORE_PRICE_LIMIT_LOW_RISK = max(MAX_CONTRACT_PRICE, 0.16)
CORE_PRICE_LIMIT_MEDIUM_RISK = max(MAX_CONTRACT_PRICE, 0.12)
CORE_PRICE_LIMIT_HIGH_RISK = max(MAX_CONTRACT_PRICE, 0.08)
CORE_PRICE_LIMIT_UNKNOWN_RISK = max(MAX_CONTRACT_PRICE, 0.10)

# Лимиты банкролла (v3.9: AI Bot style — большие позиции)
BANKROLL = float(os.getenv("BANKROLL", 75)) # Автообновление под текущий баланс
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", 0.03)) # 3% от банка на позицию (как Paris/Toronto winners)
MIN_BET_USD = float(os.getenv("MIN_BET_USD", os.getenv("MIN_ORDER_USD", 0.50)))
MAX_BET_USD = float(os.getenv("MAX_BET_USD", 25.00))
SESSION_BUDGET_PCT = float(os.getenv("SESSION_BUDGET_PCT", 0.50))
ENTRY_PRICE_BUFFER = 0.002

# HYBRID LADDER:
# Ядро всегда держим вокруг пика, а дешёвые хвосты добавляем только при высокой неопределённости.
CORE_LADDER_RADIUS = int(os.getenv("CORE_LADDER_RADIUS", 1))  # peak-1, peak, peak+1
TAIL_ENABLED = os.getenv("TAIL_ENABLED", "true").lower() == "true"
TAIL_LADDER_RADIUS = int(os.getenv("TAIL_LADDER_RADIUS", 2))  # peak±2 как дешёвый хвост
TAIL_MIN_SIGMA = float(os.getenv("TAIL_MIN_SIGMA", 2.0))
_tmp_def = (
    _entry_qb(0.018, 0.030, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.025 if AI_WEATHER else 0.01)
)
TAIL_MIN_PROB = float(os.getenv("TAIL_MIN_PROB", str(_tmp_def)))
_tme_def = (
    _entry_qb(0.048, 0.064, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.055 if AI_WEATHER else 0.01)
)
TAIL_MIN_EDGE = float(os.getenv("TAIL_MIN_EDGE", str(_tme_def)))
# Макс. |°C| от ближайшего рыночного пика до tail-страйка; 0 = без ограничения (legacy).
_tmd = os.getenv("TAIL_MAX_DIST_DEG_C", "").strip()
_tmd_def = (
    int(round(_entry_qb(7.0, 4.0, ENTRY_QUALITY_BALANCE)))
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (6 if AI_WEATHER else 0)
)
TAIL_MAX_DIST_DEG_C = int(_tmd) if _tmd else _tmd_def
# Узже cap для отдельных городов (CSV), напр. toronto — меньше «лотерейных» далёких хвостов.
_tss = os.getenv("TAIL_MAX_DIST_STRICT_CITIES", "toronto").strip().lower()
TAIL_MAX_DIST_STRICT_CITIES = frozenset(x.strip().lower() for x in _tss.split(",") if x.strip())
_tsd = os.getenv("TAIL_MAX_STRICT_DIST_DEG_C", "").strip()
TAIL_MAX_STRICT_DIST_DEG_C = int(_tsd) if _tsd else (4 if AI_WEATHER else 0)


def tail_max_dist_c_for_city(city):
    """Эффективный cap расстояния хвоста от пика (°C); 0 = не режем."""
    city_l = (city or "").strip().lower()
    cap = max(0, int(TAIL_MAX_DIST_DEG_C))
    if city_l in TAIL_MAX_DIST_STRICT_CITIES and TAIL_MAX_STRICT_DIST_DEG_C > 0:
        sc = max(0, int(TAIL_MAX_STRICT_DIST_DEG_C))
        if cap <= 0:
            return sc
        return min(cap, sc)
    return cap
TAIL_MAX_PRICE = min(MAX_CONTRACT_PRICE, 0.03)
TAIL_PRICE_LIMIT_LOW_RISK = max(TAIL_MAX_PRICE, 0.05)
TAIL_PRICE_LIMIT_MEDIUM_RISK = max(TAIL_MAX_PRICE, 0.04)
TAIL_PRICE_LIMIT_HIGH_RISK = TAIL_MAX_PRICE
TAIL_PRICE_LIMIT_UNKNOWN_RISK = max(TAIL_MAX_PRICE, 0.035)
ENABLE_NO_OVERHEAT = os.getenv("ENABLE_NO_OVERHEAT", "false").lower() in ("true", "1", "yes")
NO_MIN_CONFIDENCE = 0.85
NO_MIN_YES_PRICE = 0.12
NO_MAX_YES_PROB = 0.18
NO_MIN_PROB = 0.80
NO_MIN_EDGE = 0.06
NO_MAX_PRICE_LOW_RISK = 0.80
NO_MAX_PRICE_MEDIUM_RISK = 0.72
NO_MAX_PRICE_HIGH_RISK = 0.0
NO_MAX_PRICE_UNKNOWN_RISK = 0.0
MID_SIGMA_THRESHOLD = 2.0
HIGH_SIGMA_THRESHOLD = 3.0
HIGH_LADDER_RADIUS = 3

# Этап B (ai_weather): кап ног и усиление хвостов по σ
AI_LADDER_MAX_LEGS = int(os.getenv("AI_LADDER_MAX_LEGS", "8" if AI_WEATHER else "9"))
AI_LADDER_SIGMA_MULT = float(os.getenv("AI_LADDER_SIGMA_MULT", "1.5"))

# Гибкая лестница: пик — якорь для хвостов; без пика хвосты не берём; одиночный YES только как «якорь» при жёстких порогах.
LADDER_PEAK_REQUIRED_FOR_TAILS = os.getenv("LADDER_PEAK_REQUIRED_FOR_TAILS", "true").lower() == "true"
LADDER_ANCHOR_ONLY_ENABLED = os.getenv("LADDER_ANCHOR_ONLY_ENABLED", "true").lower() == "true"
ANCHOR_MAX_DIST_C = int(os.getenv("ANCHOR_MAX_DIST_C", "1"))
ANCHOR_ONLY_MIN_EDGE = float(os.getenv("ANCHOR_ONLY_MIN_EDGE", "0.08"))
ANCHOR_ONLY_MAX_PRICE = float(os.getenv("ANCHOR_ONLY_MAX_PRICE", "0.22"))

# Жесткие лимиты риска
MAX_EXPOSURE_PER_CITY_PCT = float(os.getenv("MAX_EXPOSURE_PER_CITY_PCT", os.getenv("MAX_PER_CITY_PCT", 0.25)))
MAX_EXPOSURE_PER_DATE_PCT = float(os.getenv("MAX_EXPOSURE_PER_DATE_PCT", os.getenv("MAX_PER_DATE_PCT", 0.35)))
_ev_raw = os.getenv("MAX_EXPOSURE_PER_EVENT_PCT", "").strip()
MAX_EXPOSURE_PER_EVENT_PCT = float(_ev_raw) if _ev_raw else MAX_EXPOSURE_PER_CITY_PCT
BLOCKED_CITIES = {
    c.strip().lower()
    for c in os.getenv("BLOCKED_CITIES", "singapore,miami,houston,atlanta,hong kong").split(",")
    if c.strip()
}
# hard = полный skip; soft = торгуем с пониженным size_multiplier (план выравнивания automatedAI)
BLOCKED_CITIES_MODE = os.getenv(
    "BLOCKED_CITIES_MODE", "soft" if AI_WEATHER else "hard"
).strip().lower()
BLOCKED_CITIES_SOFT_STAKE_MULT = float(os.getenv("BLOCKED_CITIES_SOFT_STAKE_MULT", "0.25"))

# Горячий скан: при малых часах до экспирации чаще опрашивать (только ai_weather)
HOT_HOURS_BEFORE_EXPIRY = float(os.getenv("HOT_HOURS_BEFORE_EXPIRY", "8"))

# Мин. ликвидность события (Polymarket `event.liquidity`, USD) — ниже не торгуем: тонкий стакан, шире спред, хуже выход.
MIN_EVENT_LIQUIDITY_USD = float(os.getenv("MIN_EVENT_LIQUIDITY_USD", "50"))

# Бакеты or higher / or below (фаза 1.1 плана выравнивания)
ENABLE_BUCKET_MARKETS = os.getenv("ENABLE_BUCKET_MARKETS", "true" if AI_WEATHER else "false").lower() in (
    "true",
    "1",
    "yes",
)
_bmem_def = (
    _entry_qb(1.10, 1.26, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (1.20 if AI_WEATHER else 1.0)
)
BUCKET_MIN_EDGE_MULT = float(os.getenv("BUCKET_MIN_EDGE_MULT", str(_bmem_def)))

# GFS Настройки
USE_GFS = True
GFS_OUTLIER_TRIM = 0.10  # Отбрасываем 10% крайних значений
GFS_MAX_DRIFT = 2.0  # Максимальный сдвиг пика между запусками (°C)
AGGRESSIVE_EDGE_THRESHOLD = 0.30 # Если Edge > 30%, включаем агрессивный режим
AGGRESSIVE_MIN_CONFIDENCE = float(os.getenv("AGGRESSIVE_MIN_CONFIDENCE", 0.88))
AGGRESSIVE_MAX_SIGNALS_PER_SCAN = int(os.getenv("AGGRESSIVE_MAX_SIGNALS_PER_SCAN", 2))
AGGRESSIVE_PRICE_BUFFER = float(os.getenv("AGGRESSIVE_PRICE_BUFFER", 0.006))
AGGRESSIVE_REPRICE_STEP = float(os.getenv("AGGRESSIVE_REPRICE_STEP", 0.004))
PENDING_ORDER_TTL_SEC = int(os.getenv("PENDING_ORDER_TTL_SEC", 8 * 60))
PENDING_ORPHAN_TTL_SEC = int(os.getenv("PENDING_ORPHAN_TTL_SEC", 15 * 60))
MAX_PENDING_REPRICES = int(os.getenv("MAX_PENDING_REPRICES", 2))
ORDERBOOK_WS_ENABLED = os.getenv("ORDERBOOK_WS_ENABLED", "true").lower() == "true"
ORDERBOOK_WS_MAX_ASSETS = int(os.getenv("ORDERBOOK_WS_MAX_ASSETS", 160))
ORDERBOOK_WS_STALE_SEC = int(os.getenv("ORDERBOOK_WS_STALE_SEC", 20))
ORDERBOOK_WS_RECONNECT_SEC = int(os.getenv("ORDERBOOK_WS_RECONNECT_SEC", 5))
GFS_STD = 2.5  # Стандартное отклонение для edge модели (°C) — NOAA GFS + natural variation

# City Stop-Loss
CITY_STOP_LOSS = 3  # 3 поражения подряд → блок города
CITY_STOP_LOSS_DAYS = 7  # Блок на 7 дней

# Exit rules (этап C: take-profit по цене, схлопывание edge, инвалидация модели)
ENABLE_AUTO_EXITS = os.getenv("ENABLE_AUTO_EXITS", "true").lower() == "true"
# On-chain redeem после резолва (CTF / NegRisk adapter — логика в poly_ctf_redeem.py)
ENABLE_AUTO_REDEEM = os.getenv("ENABLE_AUTO_REDEEM", "false").lower() in ("true", "1", "yes")
# За один run(): не больше стольких on-chain redeem tx (газ); 1 = по одной позиции за цикл, без «пачки»
AUTO_REDEEM_MAX_PER_SCAN = max(0, int(os.getenv("AUTO_REDEEM_MAX_PER_SCAN", "1")))
# Мин. секунд между попытками auto-redeem (0 = без ограничения). Смягчает спам при частых run().
AUTO_REDEEM_MIN_INTERVAL_SEC = max(0, int(os.getenv("AUTO_REDEEM_MIN_INTERVAL_SEC", "600")))
AUTO_REDEEM_WEATHER_ONLY = os.getenv("AUTO_REDEEM_WEATHER_ONLY", "true").lower() in ("true", "1", "yes")
_ar_min = os.getenv("AUTO_REDEEM_MIN_PAYOUT_USD", "1.0").strip()
try:
    AUTO_REDEEM_MIN_PAYOUT_USD = max(0.0, float(_ar_min)) if _ar_min else 0.0
except ValueError:
    AUTO_REDEEM_MIN_PAYOUT_USD = 1.0
try:
    AUTO_REDEEM_MAX_POSITION_AGE_DAYS = max(0, int(os.getenv("AUTO_REDEEM_MAX_POSITION_AGE_DAYS", "0") or 0))
except ValueError:
    AUTO_REDEEM_MAX_POSITION_AGE_DAYS = 0
# SELL на CLOB «мёртвых» позиций: прошёл endDate, redeemable=false, curPrice ≤ порога (как positions_losers_report discard)
ENABLE_AUTO_DUMP_DEAD = os.getenv("ENABLE_AUTO_DUMP_DEAD", "false").lower() in ("true", "1", "yes")
AUTO_DUMP_DEAD_MAX_PER_SCAN = max(0, int(os.getenv("AUTO_DUMP_DEAD_MAX_PER_SCAN", "1")))
AUTO_DUMP_DEAD_MIN_INTERVAL_SEC = max(0, int(os.getenv("AUTO_DUMP_DEAD_MIN_INTERVAL_SEC", "300")))
AUTO_DUMP_DEAD_WEATHER_ONLY = os.getenv("AUTO_DUMP_DEAD_WEATHER_ONLY", "true").lower() in ("true", "1", "yes")
AUTO_DUMP_DEAD_MAX_CUR = float(os.getenv("AUTO_DUMP_DEAD_MAX_CUR", "0.02"))
# Мин. ожидаемая выручка bid×size до отправки SELL; иначе пыль ($0.00–0.01) и убыток на газ/время. 0 = выкл.
AUTO_DUMP_DEAD_MIN_NOTIONAL_USD = max(
    0.0, float(os.getenv("AUTO_DUMP_DEAD_MIN_NOTIONAL_USD", "0.3"))
)
# Мин. bid до размещения dump (доля 0–1); ниже — только пыль, round(float,4) мог давать 0
AUTO_DUMP_DEAD_MIN_BID = float(os.getenv("AUTO_DUMP_DEAD_MIN_BID", "0.0001"))
ENABLE_TAKE_PROFIT_EXIT = os.getenv("ENABLE_TAKE_PROFIT_EXIT", "true").lower() == "true"
ENABLE_TIME_EXIT = os.getenv("ENABLE_TIME_EXIT", "true").lower() == "true"
MODEL_INVALIDATION_SELL = os.getenv("MODEL_INVALIDATION_SELL", "true" if AI_WEATHER else "false").lower() == "true"
SELL_ON_EDGE_COLLAPSE = os.getenv("SELL_ON_EDGE_COLLAPSE", "true" if AI_WEATHER else "false").lower() == "true"
ENABLE_FORECAST_SHIFT_EXIT = os.getenv("ENABLE_FORECAST_SHIFT_EXIT", str(MODEL_INVALIDATION_SELL)).lower() == "true"
ENABLE_EDGE_EXIT = os.getenv("ENABLE_EDGE_EXIT", str(SELL_ON_EDGE_COLLAPSE)).lower() == "true"
AUTO_EXIT_ONLY_IN_PROFIT = os.getenv("AUTO_EXIT_ONLY_IN_PROFIT", "true").lower() == "true"
# Порог PnL (доля прибыли vs avg) для take_profit_partial: exact-страйк = round(peak)±радиус (как якорь HOLD)
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", 1.0))
# Хвосты, бакеты и exact далеко от peak — фиксация при меньшем PnL
TAKE_PROFIT_PCT_NON_ANCHOR = float(os.getenv("TAKE_PROFIT_PCT_NON_ANCHOR", "0.35"))
TAKE_PROFIT_SELL_FRACTION = float(os.getenv("TAKE_PROFIT_SELL_FRACTION", 0.5))
ENABLE_TAKE_PROFIT_PRICE_LEVELS = os.getenv("ENABLE_TAKE_PROFIT_PRICE_LEVELS", "true" if AI_WEATHER else "false").lower() == "true"
_DEFAULT_TP_LEVELS = [0.10, 0.16, 0.25, 0.40] if AI_WEATHER else [0.15, 0.35, 0.55]
TAKE_PROFIT_PRICE_LEVELS = sorted(_parse_csv_floats(os.getenv("TAKE_PROFIT_PRICE_LEVELS", ""), _DEFAULT_TP_LEVELS))
TAKE_PROFIT_PRICE_LEVEL_SELL_FRACTION = float(os.getenv("TAKE_PROFIT_PRICE_LEVEL_SELL_FRACTION", "0.30"))
# Опционально: фиксация когда bid >= K * средняя цена входа (сеточный стиль)
TAKE_PROFIT_BID_VS_AVG_MULT = float(
    os.getenv("TAKE_PROFIT_BID_VS_AVG_MULT", "2.5" if AI_WEATHER else "0")
)
EXIT_EDGE_BUFFER = float(os.getenv("EXIT_EDGE_BUFFER", "0.01"))
EXIT_CLOSE_HOURS = float(os.getenv("EXIT_CLOSE_HOURS", 8))
TIME_EXIT_MIN_PNL_PCT = float(os.getenv("TIME_EXIT_MIN_PNL_PCT", 0.0))
EXIT_OUTSIDE_CORE = os.getenv("EXIT_OUTSIDE_CORE", "true").lower() == "true"

# Якорь до резолва: exact-YES, чей страйк = round(peak GFS) ± радиус, не трогаем авто-выходом
# (остальные линии на тот же город/дату — фиксируем по TP / bid×avg / time / edge как раньше).
EXIT_HOLD_FORECAST_ANCHOR = os.getenv(
    "EXIT_HOLD_FORECAST_ANCHOR", "true" if AI_WEATHER else "false"
).lower() in ("true", "1", "yes")
EXIT_ANCHOR_RADIUS_C = int(os.getenv("EXIT_ANCHOR_RADIUS_C", "0"))
# Если bid уже почти 1.0 — всё равно разрешаем выход (фиксация перед резолвом).
EXIT_ANCHOR_BYPASS_BID = float(os.getenv("EXIT_ANCHOR_BYPASS_BID", "0.90"))
# Только частичные авто-SELL: не исполнять «пыль» (~$0.17–0.20); поднимаем объём до мин. выручки.
# Полный выход (100% позиции) всегда разрешён — иначе при отсутствии ручной продажи остаток «замрёт».
# MIN_EXIT_NOTIONAL_USD приоритет; иначе legacy MIN_PARTIAL_EXIT_USD. 0 = выкл.
MIN_EXIT_NOTIONAL_USD = float(
    os.getenv("MIN_EXIT_NOTIONAL_USD", os.getenv("MIN_PARTIAL_EXIT_USD", "0.5"))
)

# Polymarket: мин. 5 контрактов и ~$1 notional — подтягиваем размер ордера (этап A)
POLY_MIN_CONTRACTS = int(os.getenv("POLY_MIN_CONTRACTS", "5"))
POLY_MIN_ORDER_USD = float(os.getenv("POLY_MIN_ORDER_USD", "1.0"))
POLY_AUTO_BUMP_TO_MIN = os.getenv("POLY_AUTO_BUMP_TO_MIN", "true").lower() == "true"
# CLOB для marketable BUY иногда отклоняет ордер, если внутренний notional чуть ниже $1 (округления).
POLY_MARKETABLE_BUFFER_USD = float(os.getenv("POLY_MARKETABLE_BUFFER_USD", "0.03"))

# Макс. разных exact/bucket YES на один город и дату события (новые покупки за скан); 0 = без лимита
MAX_STRIKES_PER_CITY_DATE = int(os.getenv("MAX_STRIKES_PER_CITY_DATE", str(3 if AI_WEATHER else 0)))

# Макс. разных exact (не encoded bucket) на город+дату уже в cache; 0 = выкл.
MAX_EXACT_TEMPS_PER_CITY_DATE = int(
    os.getenv("MAX_EXACT_TEMPS_PER_CITY_DATE", str(2 if AI_WEATHER else 0))
)
ENFORCE_MAX_EXACT_TEMPS_PER_CITY_DATE = os.getenv(
    "ENFORCE_MAX_EXACT_TEMPS_PER_CITY_DATE", "true" if AI_WEATHER else "false"
).lower() in ("true", "1", "yes")

# Сверка purchases.json с API: по умолчанию ВКЛ — убирает записи по token_id, которых нет среди открытых позиций (продано).
# Выключите (false), если не хотите никакой автоматической подчистки по API.
RECONCILE_PURCHASES_WITH_API = os.getenv("RECONCILE_PURCHASES_WITH_API", "true").lower() in (
    "true",
    "1",
    "yes",
)
# Только если API вернул пустой список позиций [] при том что в purchases есть token_id — без этого флага не чистим
# (защита от сбоя API и повторных покупок). Если реально продали всё, можно выставить true на один запуск.
RECONCILE_TRUST_EMPTY_POSITIONS_API = os.getenv("RECONCILE_TRUST_EMPTY_POSITIONS_API", "false").lower() in (
    "true",
    "1",
    "yes",
)

# Макс. исполнений за один скан (YES+NO)
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "10"))

# Scan speed
HOT_SCAN_INTERVAL_SEC = int(os.getenv("HOT_SCAN_INTERVAL_SEC", 120))
FAST_SCAN_INTERVAL_SEC = int(os.getenv("FAST_SCAN_INTERVAL_SEC", 300))
STREAMING_READY = False

# Telegram: только уведомления о сделках/покупках (send_telegram); сводка позиций и закреп в чате убраны
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TG_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"

# Цикличный режим с GFS-привязкой (как AI Trading Bot)
AUTO_SCAN = os.getenv("AUTO_SCAN", "false").lower() == "true"
AUTO_SCAN_INTERVAL = int(os.getenv("AUTO_SCAN_INTERVAL", "0") or 0)

# 🌡️ GFS SCHEDULE — AI Bot покупает СРАЗУ после обновления GFS!
# GFS runs: 00:00, 06:00, 12:00, 18:00 UTC
# Данные доступны через 3-6 часов после run
GFS_RUNS_UTC = [0, 6, 12, 18]  # Часы GFS runs
GFS_DATA_DELAY_HOURS = 3  # Задержка данных после GFS run
GFS_ACTIVE_WINDOW_HOURS = 12  # Считаем свежие 12ч (увеличено)
OPEN_METEO_RATE_LIMIT_BACKOFF_SEC = 300
# Мин. пауза между HTTP-запросами к api.open-meteo.com (снижает 429 при массовых вызовах get_gfs_forecast)
OPEN_METEO_MIN_REQUEST_INTERVAL_SEC = max(
    0.0, float(os.getenv("OPEN_METEO_MIN_REQUEST_INTERVAL_SEC", "0.45"))
)
# Параллельные GET к gamma-api по slug (было 500+ последовательных запросов — узкое место скана).
GAMMA_SLUG_FETCH_WORKERS = max(1, min(64, int(os.getenv("GAMMA_SLUG_FETCH_WORKERS", "24"))))
GAMMA_SLUG_FETCH_TIMEOUT_SEC = max(3, int(os.getenv("GAMMA_SLUG_FETCH_TIMEOUT_SEC", "10")))
FORECAST_CACHE_TTL_SEC = 6 * 3600
METNO_USER_AGENT = os.getenv("METNO_USER_AGENT", "weather-bot-v4/1.0 (Cursor local bot)")

# Интервалы сканирования — по умолчанию 10 мин, но GUI может переопределить через .env
SCAN_INTERVAL_GFS_ACTIVE = AUTO_SCAN_INTERVAL or 600
SCAN_INTERVAL_GFS_NORMAL = AUTO_SCAN_INTERVAL or 600
SCAN_INTERVAL_GFS_IDLE = AUTO_SCAN_INTERVAL or 600

# Полный список городов для температурных рынков Polymarket:
# (канонический ключ = CITY_COORDS / GFS, polymarket_slug = сегмент в highest-temperature-in-{slug}-on-...,
#  extra_needles — доп. подстроки в title, напр. «nyc» вместо «new york»).
WEATHER_TRADING_CITIES = [
    ("london", "london", ()),
    ("paris", "paris", ()),
    ("seoul", "seoul", ()),
    ("tokyo", "tokyo", ()),
    ("shanghai", "shanghai", ()),
    ("new york", "nyc", ("nyc",)),  # Polymarket slug nyc, в title часто «NYC»
    ("chicago", "chicago", ()),
    ("munich", "munich", ()),
    ("seattle", "seattle", ()),
    ("singapore", "singapore", ()),
    ("miami", "miami", ()),
    ("atlanta", "atlanta", ()),
    ("houston", "houston", ()),
    ("beijing", "beijing", ()),
    ("hong kong", "hong-kong", ()),
    ("amsterdam", "amsterdam", ()),
    ("madrid", "madrid", ()),
    ("shenzhen", "shenzhen", ()),
    ("moscow", "moscow", ()),
    ("istanbul", "istanbul", ()),
    ("ankara", "ankara", ()),
    ("busan", "busan", ()),
    ("toronto", "toronto", ()),
    ("lucknow", "lucknow", ()),
    ("chengdu", "chengdu", ()),
    ("taipei", "taipei", ()),
    ("wuhan", "wuhan", ()),
    ("wellington", "wellington", ()),
    ("dallas", "dallas", ()),
    ("denver", "denver", ()),
    ("los angeles", "los-angeles", ()),
    ("san francisco", "san-francisco", ()),
    ("mexico city", "mexico-city", ()),
    ("sao paulo", "sao-paulo", ()),
    ("buenos aires", "buenos-aires", ()),
    ("cape town", "cape-town", ()),
    ("tel aviv", "tel-aviv", ()),
    ("warsaw", "warsaw", ()),
    ("milan", "milan", ()),
    ("jeddah", "jeddah", ()),
    ("lagos", "lagos", ()),
    ("panama city", "panama-city", ()),
    ("berlin", "berlin", ()),
    ("dubai", "dubai", ()),
    ("sydney", "sydney", ()),
    ("melbourne", "melbourne", ()),
    ("rome", "rome", ()),
    ("phoenix", "phoenix", ()),
    ("boston", "boston", ()),
    ("philadelphia", "philadelphia", ()),
    ("vancouver", "vancouver", ()),
    ("montreal", "montreal", ()),
    ("detroit", "detroit", ()),
    ("minneapolis", "minneapolis", ()),
    ("san diego", "san-diego", ()),
    ("lisbon", "lisbon", ()),
    ("athens", "athens", ()),
    ("prague", "prague", ()),
    ("vienna", "vienna", ()),
    ("brussels", "brussels", ()),
    ("dublin", "dublin", ()),
    ("cairo", "cairo", ()),
    ("riyadh", "riyadh", ()),
    ("doha", "doha", ()),
    ("manila", "manila", ()),
    ("jakarta", "jakarta", ()),
    ("kuala lumpur", "kuala-lumpur", ()),
    ("bangkok", "bangkok", ()),
    ("delhi", "delhi", ()),
    ("mumbai", "mumbai", ()),
    ("bangalore", "bangalore", ()),
    ("nairobi", "nairobi", ()),
    ("johannesburg", "johannesburg", ()),
    ("auckland", "auckland", ()),
]


def resolve_trading_city_from_title(title_lower: str) -> str | None:
    """Сопоставить заголовок события/позиции с каноническим ключом города (самая длинная подстрока выигрывает)."""
    if not title_lower:
        return None
    best_canon = None
    best_len = -1
    for canon, _slug, extras in WEATHER_TRADING_CITIES:
        for needle in (canon,) + tuple(extras):
            if needle and needle in title_lower and len(needle) > best_len:
                best_len = len(needle)
                best_canon = canon
    return best_canon


PRIORITY_CITIES = tuple(t[0] for t in WEATHER_TRADING_CITIES)
POLYMARKET_TEMPERATURE_SLUGS = tuple(t[1] for t in WEATHER_TRADING_CITIES)

# Центры городов (грубо; для прогноза при FORECAST_COORDS_MODE=city_center). Станции резолва — ниже.
CITY_COORDS = {
    "london": (51.507, -0.128),
    "paris": (48.857, 2.352),
    "seoul": (37.566, 126.978),
    "tokyo": (35.676, 139.650),
    "shanghai": (31.230, 121.473),
    "new york": (40.713, -74.006),
    "chicago": (41.878, -87.630),
    "munich": (48.137, 11.575),
    "seattle": (47.606, -122.332),
    "singapore": (1.352, 103.820),
    "miami": (25.762, -80.192),
    "atlanta": (33.749, -84.388),
    "houston": (29.760, -95.370),
    "beijing": (39.905, 116.391),
    "hong kong": (22.319, 114.169),
    "amsterdam": (52.367, 4.904),
    "madrid": (40.416, -3.703),
    "moscow": (55.755, 37.617),
    "istanbul": (41.008, 28.978),
    "ankara": (39.933, 32.859),
    "busan": (35.180, 129.075),
    "shenzhen": (22.543, 114.058),
    "toronto": (43.653, -79.383),
    "lucknow": (26.847, 80.946),
    "chengdu": (30.572, 104.066),
    "taipei": (25.033, 121.565),
    "wuhan": (30.593, 114.305),
    "wellington": (-41.286, 174.776),
    "dallas": (32.777, -96.797),
    "denver": (39.739, -104.990),
    "los angeles": (34.052, -118.244),
    "san francisco": (37.775, -122.419),
    "mexico city": (19.433, -99.133),
    "sao paulo": (-23.551, -46.633),
    "buenos aires": (-34.604, -58.382),
    "cape town": (-33.925, 18.424),
    "tel aviv": (32.085, 34.782),
    "warsaw": (52.230, 21.011),
    "milan": (45.464, 9.190),
    "jeddah": (21.485, 39.192),
    "lagos": (6.524, 3.379),
    "panama city": (8.982, -79.520),
    "berlin": (52.520, 13.405),
    "dubai": (25.205, 55.271),
    "sydney": (-33.869, 151.209),
    "melbourne": (-37.814, 144.963),
    "rome": (41.903, 12.496),
    "phoenix": (33.448, -112.074),
    "boston": (42.361, -71.057),
    "philadelphia": (39.953, -75.164),
    "vancouver": (49.283, -123.121),
    "montreal": (45.502, -73.567),
    "detroit": (42.331, -83.046),
    "minneapolis": (44.978, -93.265),
    "san diego": (32.715, -117.161),
    "lisbon": (38.723, -9.139),
    "athens": (37.984, 23.728),
    "prague": (50.075, 14.438),
    "vienna": (48.209, 16.373),
    "brussels": (50.850, 4.352),
    "dublin": (53.349, -6.260),
    "cairo": (30.045, 31.236),
    "riyadh": (24.714, 46.675),
    "doha": (25.286, 51.533),
    "manila": (14.599, 120.984),
    "jakarta": (-6.175, 106.827),
    "kuala lumpur": (3.139, 101.687),
    "bangkok": (13.756, 100.502),
    "delhi": (28.614, 77.209),
    "mumbai": (19.076, 72.878),
    "bangalore": (12.972, 77.595),
    "nairobi": (-1.292, 36.822),
    "johannesburg": (-26.205, 28.047),
    "auckland": (-36.849, 174.763),
}

# Координаты станций резолва: по возможности та же точка, что и у Weather Underground в правилах Polymarket.
# В описании рынка часто указано: «resolution source … Wunderground … station …», со ссылкой вида
#   https://www.wunderground.com/history/daily/<country>/<place>/<ICAO>
# Примеры: https://www.wunderground.com/history/daily/es/madrid/LEMD (LEMD),
# https://www.wunderground.com/history/daily/jp/tokyo/RJTT (RJTT Haneda).
# Lat/lon в шапке страницы WU — ориентир для строк ниже; Open-Meteo интерполирует сетку к этим координатам.
# Дополняйте POLYMARKET_WU_RESOLUTION_ICAO из текста правил конкретного рынка при расхождении.
# FORECAST_COORDS_MODE=city_center — только центр города (CITY_COORDS), без привязки к станции.
FORECAST_COORDS_MODE = os.getenv("FORECAST_COORDS_MODE", "resolution").strip().lower()
# ICAO станции резолва (как в URL WU / правилах Polymarket); координаты ниже — опорные точки аэродромов ARP.
POLYMARKET_WU_RESOLUTION_ICAO = {
    "london": "EGLC",
    "paris": "LFPG",
    "seoul": "RKSI",
    "tokyo": "RJTT",
    "shanghai": "ZSPD",
    "new york": "KLGA",
    "chicago": "KORD",
    "munich": "EDDM",
    "seattle": "KSEA",
    "singapore": "WSSS",
    "miami": "KMIA",
    "atlanta": "KATL",
    "houston": "KIAH",
    "beijing": "ZBAA",
    "hong kong": "VHHH",
    "amsterdam": "EHAM",
    "madrid": "LEMD",
    "moscow": "UUEE",
    "istanbul": "LTFM",
    "ankara": "LTAC",
    "busan": "RKPK",
    "shenzhen": "ZGSZ",
    "toronto": "CYYZ",
    "lucknow": "VILK",
    "chengdu": "ZUUU",
    "taipei": "RCTP",
    "wuhan": "ZHHH",
    "wellington": "NZWN",
    "dallas": "KDFW",
    "denver": "KDEN",
    "los angeles": "KLAX",
    "san francisco": "KSFO",
    "mexico city": "MMMX",
    "sao paulo": "SBGR",
    "buenos aires": "SAEZ",
    "cape town": "FACT",
    "tel aviv": "LLBG",
    "warsaw": "EPWA",
    "milan": "LIMC",
    "jeddah": "OEJN",
    "lagos": "DNMM",
    "panama city": "MPTO",
    "berlin": "EDDB",
    "dubai": "OMDB",
    "sydney": "YSSY",
    "melbourne": "YMML",
    "rome": "LIRF",
    "phoenix": "KPHX",
    "boston": "KBOS",
    "philadelphia": "KPHL",
    "vancouver": "CYVR",
    "montreal": "CYUL",
    "detroit": "KDTW",
    "minneapolis": "KMSP",
    "san diego": "KSAN",
    "lisbon": "LPPT",
    "athens": "LGAV",
    "prague": "LKPR",
    "vienna": "LOWW",
    "brussels": "EBBR",
    "dublin": "EIDW",
    "cairo": "HECA",
    "riyadh": "OERK",
    "doha": "OTHH",
    "manila": "RPLL",
    "jakarta": "WIII",
    "kuala lumpur": "WMKK",
    "bangkok": "VTBS",
    "delhi": "VIDP",
    "mumbai": "VABB",
    "bangalore": "VOBL",
    "nairobi": "HKJK",
    "johannesburg": "FAOR",
    "auckland": "NZAA",
}
RESOLUTION_STATION_COORDS = {
    "london": (51.5053, 0.0553),       # EGLC London City (Polymarket / WU)
    "paris": (49.0097, 2.5479),       # LFPG CDG
    "seoul": (37.4692, 126.4505),     # RKSI Incheon
    "tokyo": (35.5500, 139.7800),     # RJTT Haneda (как на WU RJTT)
    "shanghai": (31.1439, 121.8053),  # ZSPD Pudong
    "new york": (40.7769, -73.8740),  # KLGA LaGuardia
    "chicago": (41.9742, -87.9073),   # KORD O'Hare
    "munich": (48.3538, 11.7861),     # EDDM
    "seattle": (47.4490, -122.3090),  # KSEA
    "singapore": (1.3644, 103.9915),  # WSSS Changi
    "miami": (25.7959, -80.2875),     # KMIA
    "atlanta": (33.6407, -84.4277),   # KATL
    "houston": (29.9844, -95.3414),   # KIAH
    "beijing": (40.0801, 116.5846),   # ZBAA Capital
    "hong kong": (22.3089, 113.9186), # VHHH
    "amsterdam": (52.3105, 4.7683),   # EHAM Schiphol
    "madrid": (40.4500, -3.5800),     # LEMD Barajas (как на WU LEMD)
    "moscow": (55.9726, 37.4146),     # UUEE Sheremetyevo
    "istanbul": (41.2753, 28.7519),   # LTFM
    "ankara": (40.1281, 32.9951),     # LTAC Esenboğa
    "busan": (35.1795, 129.0756),     # RKPK Gimhae
    "shenzhen": (22.6392, 113.8108),  # ZGSZ Bao'an
    "toronto": (43.6777, -79.6248),   # CYYZ Pearson
    "lucknow": (26.7606, 80.8893),    # VILK Amausi
    "chengdu": (30.5785, 103.9471),   # ZUUU Shuangliu
    "taipei": (25.0797, 121.2342),    # RCTP Taoyuan
    "wuhan": (30.7738, 114.2081),     # ZHHH Tianhe
    "wellington": (-41.3272, 174.8054), # NZWN
    "dallas": (32.8968, -97.0380),    # KDFW
    "denver": (39.8617, -104.6737),   # KDEN
    "los angeles": (33.9425, -118.4081), # KLAX
    "san francisco": (37.6213, -122.3790), # KSFO
    "mexico city": (19.4364, -99.0720), # MMMX
    "sao paulo": (-23.4356, -46.4731), # SBGR GRU
    "buenos aires": (-34.8222, -58.5358), # SAEZ EZE
    "cape town": (-33.9648, 18.6017), # FACT
    "tel aviv": (32.0114, 34.8867),   # LLBG
    "warsaw": (52.1657, 20.9671),     # EPWA
    "milan": (45.6306, 8.7281),      # LIMC MXP
    "jeddah": (21.6796, 39.1565),     # OEJN
    "lagos": (6.5774, 3.3212),        # DNMM LOS
    "panama city": (8.9147, -79.5996), # MPTO PTY
    "berlin": (52.3664, 13.5033),     # EDDB BER
    "dubai": (25.2528, 55.3644),      # OMDB DXB
    "sydney": (-33.9461, 151.1772),   # YSSY
    "melbourne": (-37.6733, 144.8431), # YMML
    "rome": (41.8006, 12.2389),      # LIRF FCO
    "phoenix": (33.4343, -112.0114),  # KPHX
    "boston": (42.3650, -71.0053),   # KBOS
    "philadelphia": (39.8719, -75.2411), # KPHL
    "vancouver": (49.1939, -123.1844), # CYVR
    "montreal": (45.4706, -73.7408),  # CYUL
    "detroit": (42.2122, -83.3534),   # KDTW
    "minneapolis": (44.8844, -93.2228), # KMSP
    "san diego": (32.7336, -117.1903), # KSAN
    "lisbon": (38.7814, -9.1359),     # LPPT
    "athens": (37.9367, 23.9475),     # LGAV
    "prague": (50.1008, 14.2603),     # LKPR
    "vienna": (48.1103, 16.5697),     # LOWW
    "brussels": (50.9014, 4.4844),   # EBBR
    "dublin": (53.4214, -6.2700),    # EIDW
    "cairo": (30.1219, 31.4056),     # HECA
    "riyadh": (24.9581, 46.7008),     # OERK RUH
    "doha": (25.2731, 51.6081),       # OTHH
    "manila": (14.5086, 121.0198),    # RPLL MNL
    "jakarta": (-6.1256, 106.6558),   # WIII CGK
    "kuala lumpur": (2.7456, 101.7100), # WMKK KUL
    "bangkok": (13.6928, 100.7510),   # VTBS BKK
    "delhi": (28.5569, 77.1011),      # VIDP DEL
    "mumbai": (19.0886, 72.8679),     # VABB BOM
    "bangalore": (13.1989, 77.7056), # VOBL BLR
    "nairobi": (-1.3192, 36.9278),    # HKJK NBO
    "johannesburg": (-26.1392, 28.2461), # FAOR JNB
    "auckland": (-37.0082, 174.7920), # NZAA AKL
}


def get_forecast_coordinates(city_key: str):
    """
    lat, lon для Open-Meteo / met.no.
    Режим resolution: станция из RESOLUTION_STATION_COORDS (по возможности совпадает со станцией WU из правил Polymarket),
    иначе fallback на CITY_COORDS.
    Режим city_center: всегда CITY_COORDS.
    """
    city_lower = (city_key or "").strip().lower()
    if not city_lower:
        return None
    if FORECAST_COORDS_MODE == "city_center":
        c = CITY_COORDS.get(city_lower)
        if not c:
            return None
        return c[0], c[1], "city_center"
    if city_lower in RESOLUTION_STATION_COORDS:
        lat, lon = RESOLUTION_STATION_COORDS[city_lower]
        return lat, lon, "resolution_station"
    c = CITY_COORDS.get(city_lower)
    if not c:
        return None
    return c[0], c[1], "city_center_fallback"


# Исторические risk tiers и city-specific model weights — калибровка 2025 по двум прогонам:
# calibrate_openmeteo_vs_bot (ERA5) + calibrate_openmeteo_vs_wu_resolution (Meteostat ≈ WU ICAO).
# LOW/HIGH: перцентили p25/p75 по композиту (2/3 station MAE + 1/3 ERA MAE), только города с полным station ensemble;
# в HIGH добавлены legacy волатильные рынки (NYC, ATL, MIA, WLG, Busan).
MEDIUM_RISK_CITIES = frozenset()  # зарезервировано; medium = всё не low/high/no_wu в get_city_risk_tier
LOW_RISK_CITIES = {
    "amsterdam",
    "athens",
    "auckland",
    "bangalore",
    "berlin",
    "hong kong",
    "jakarta",
    "lisbon",
    "london",
    "moscow",
    "nairobi",
    "paris",
    "prague",
    "riyadh",
    "rome",
    "singapore",
    "tokyo",
    "vienna",
}
HIGH_RISK_CITIES = {
    "atlanta",
    "buenos aires",
    "busan",
    "delhi",
    "denver",
    "dublin",
    "istanbul",
    "johannesburg",
    "kuala lumpur",
    "los angeles",
    "lucknow",
    "manila",
    "melbourne",
    "miami",
    "new york",
    "phoenix",
    "san diego",
    "seoul",
    "wellington",
}
NO_WU_DATA_CITIES = {"beijing", "chengdu", "shanghai", "wuhan"}

# Модель существенно лучше ensemble на станции (MAE↓ ≥ 0.08 °C) → отдельный профиль весов
GFS_FAVORED_CITIES = {
    "hong kong",
    "los angeles",
    "melbourne",
    "san diego",
}
ECMWF_FAVORED_CITIES = {
    "cairo",
    "istanbul",
    "johannesburg",
    "kuala lumpur",
    "moscow",
    "phoenix",
}
BOM_FAVORED_CITIES = {
    "dublin",
    "panama city",
    "seoul",
}

DEFAULT_MODEL_WEIGHTS = {
    "gfs_global": 0.40,
    "ecmwf_ifs025": 0.25,
    "ukmo_seamless": 0.10,
    "bom_access": 0.10,
    "metno_locationforecast": 0.15,
}
GFS_MODEL_WEIGHTS = {
    "gfs_global": 0.50,
    "ecmwf_ifs025": 0.20,
    "ukmo_seamless": 0.10,
    "bom_access": 0.10,
    "metno_locationforecast": 0.10,
}
ECMWF_MODEL_WEIGHTS = {
    "gfs_global": 0.25,
    "ecmwf_ifs025": 0.45,
    "ukmo_seamless": 0.10,
    "bom_access": 0.10,
    "metno_locationforecast": 0.10,
}
BOM_MODEL_WEIGHTS = {
    "gfs_global": 0.30,
    "ecmwf_ifs025": 0.20,
    "ukmo_seamless": 0.10,
    "bom_access": 0.30,
    "metno_locationforecast": 0.10,
}

_csk_def = (
    _entry_qb(0.40, 0.52, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.45 if AI_WEATHER else 0.35)
)
CONFIDENCE_SKIP_THRESHOLD = float(os.getenv("CONFIDENCE_SKIP_THRESHOLD", str(_csk_def)))
_crs_def = (
    _entry_qb(0.58, 0.66, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.62 if AI_WEATHER else 0.55)
)
CONFIDENCE_REDUCED_SIZE_THRESHOLD = float(os.getenv("CONFIDENCE_REDUCED_SIZE_THRESHOLD", str(_crs_def)))
_ctl_def = (
    _entry_qb(0.74, 0.82, ENTRY_QUALITY_BALANCE)
    if AI_WEATHER and ENTRY_QUALITY_BALANCE is not None
    else (0.78 if AI_WEATHER else 0.75)
)
CONFIDENCE_TAILS_THRESHOLD = float(os.getenv("CONFIDENCE_TAILS_THRESHOLD", str(_ctl_def)))
MIN_BIAS_BUCKET_COUNT = 3
MAX_BIAS_ERRORS_PER_BUCKET = 60

# Цвета для консоли
class C:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_green(text):
    print(f"{C.GREEN}{text}{C.ENDC}")

def log_yellow(text):
    print(f"{C.WARNING}{text}{C.ENDC}")

def log_blue(text):
    print(f"{C.BLUE}{text}{C.ENDC}")

# ═══════════════════════════════════════════════════════════════════
# 🔧 ИНИЦИАЛИЗАЦИЯ КЛИЕНТА И РЕАЛЬНАЯ ПОКУПКА
# ═══════════════════════════════════════════════════════════════════

def init_client(private_key, funder):
    """Инициализирует ClobClient для реальных покупок"""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON
    
    temp = ClobClient(host="https://clob.polymarket.com", chain_id=POLYGON, key=private_key)
    creds = temp.create_or_derive_api_creds()
    logger.info(f"   🔑 API creds: {creds.api_key[:8]}...")
    
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON,
        key=private_key,
        creds=ApiCreds(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase
        ),
        signature_type=0,
        funder=funder
    )


def get_wallet_balance_api(wallet):
    """Получает баланс кошелька через data-api /balances."""
    if not wallet:
        return None

    try:
        resp = requests.get(
            "https://data-api.polymarket.com/balances",
            params={"user": wallet},
            timeout=10,
            verify=False
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json() or {}
        for key in ("USDC", "USDC_e", "usdc", "balance"):
            value = data.get(key)
            if value is not None:
                return float(value)
    except Exception as e:
        logger.warning(f"   ⚠️ data-api balance ошибка: {e}")

    return None


def _json_list(value):
    """Нормализует строку JSON / list / tuple в python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [raw]
    return []


def _to_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_market_token_id(market, outcome_name="Yes"):
    """Достаёт token_id по имени исхода из market['clobTokenIds']."""
    token_ids = _json_list(market.get("clobTokenIds"))
    outcomes = _json_list(market.get("outcomes"))

    if not token_ids:
        token_id = market.get("token_id") or market.get("asset") or market.get("asset_id")
        return str(token_id) if token_id else None

    if outcomes and len(outcomes) == len(token_ids):
        for idx, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == outcome_name.lower():
                return str(token_ids[idx])

    # По умолчанию Polymarket обычно хранит [Yes, No]
    return str(token_ids[0])


def get_market_metadata_quote(market, outcome_name="Yes"):
    """
    Берёт цену напрямую из gamma market metadata.
    Для weather exact markets это обычно надёжнее, чем raw /book.
    """
    outcomes = _json_list(market.get("outcomes"))
    outcome_prices = _json_list(market.get("outcomePrices"))

    market_best_bid = _to_float(market.get("bestBid"), 0.0)
    market_best_ask = _to_float(market.get("bestAsk"), 0.0)
    market_last = _to_float(market.get("lastTradePrice", market.get("price", 0.0)), 0.0)

    outcome_price = 0.0
    if outcomes and outcome_prices and len(outcomes) == len(outcome_prices):
        for idx, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == outcome_name.lower():
                outcome_price = _to_float(outcome_prices[idx], 0.0)
                break

    return {
        "best_bid": market_best_bid,
        "best_ask": market_best_ask,
        "last_trade": market_last,
        "outcome_price": outcome_price,
    }


def fetch_clob_book(token_id):
    """Получает order book токена через CLOB API."""
    if not token_id:
        return {}

    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": str(token_id)},
            timeout=10,
            verify=False
        )
        if resp.ok:
            return resp.json() or {}
    except Exception:
        pass
    return {}


class LiveOrderbookFeed:
    """Фоновый WebSocket-кэш orderbook для активных token_id."""

    def __init__(self):
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.enabled = ORDERBOOK_WS_ENABLED
        self._thread = None
        self._loop = None
        self._ws = None
        self._lock = threading.Lock()
        self._running = False
        self._connected = False
        self._subscribed = set()
        self._books = {}

    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, name="pm-orderbook-ws", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._connected = False
        loop = self._loop
        ws = self._ws
        if loop and loop.is_running():
            if ws is not None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(ws.close(), loop)
                    fut.result(timeout=5)
                except Exception:
                    pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._ws = None

    def ensure_assets(self, asset_ids):
        if not self.enabled:
            return
        clean_ids = [str(a) for a in (asset_ids or []) if a]
        if not clean_ids:
            return
        if len(self._subscribed) >= ORDERBOOK_WS_MAX_ASSETS:
            return
        if self._thread is None:
            self.start()

        new_ids = []
        with self._lock:
            for asset_id in clean_ids:
                if asset_id in self._subscribed:
                    continue
                if len(self._subscribed) + len(new_ids) >= ORDERBOOK_WS_MAX_ASSETS:
                    break
                self._subscribed.add(asset_id)
                new_ids.append(asset_id)

        if not new_ids:
            return
        loop = self._loop
        if loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._subscribe(new_ids, replace=False), loop)
            except Exception:
                pass

    def get_book(self, asset_id):
        if not asset_id:
            return None
        with self._lock:
            snapshot = self._books.get(str(asset_id))
        if not snapshot:
            return None
        if time.time() - float(snapshot.get("received_at", 0.0)) > ORDERBOOK_WS_STALE_SEC:
            return None
        return snapshot

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_forever())
        except Exception as e:
            logger.warning(f"   ⚠️ orderbook ws stopped: {e}")
        finally:
            self._connected = False
            self._ws = None
            try:
                self._loop.close()
            except Exception:
                pass

    async def _connect_ws(self):
        try:
            try:
                from websockets.asyncio.client import connect as ws_connect
            except ImportError:
                import websockets
                ws_connect = websockets.connect
            self._ws = await ws_connect(self.url, ping_interval=20, ping_timeout=10)
            self._connected = True
            logger.info("   📡 Orderbook WS connected")
            return True
        except Exception as e:
            self._connected = False
            logger.warning(f"   ⚠️ orderbook ws connect failed: {e}")
            return False

    async def _subscribe(self, asset_ids, replace=False):
        if not self._ws or not self._connected or not asset_ids:
            return False
        payload = {"assets_ids": list(asset_ids)}
        if replace:
            payload["type"] = "MARKET"
        else:
            payload["operation"] = "subscribe"
        try:
            await self._ws.send(json.dumps(payload))
            return True
        except Exception as e:
            logger.warning(f"   ⚠️ orderbook ws subscribe failed: {e}")
            return False

    async def _resubscribe_all(self):
        with self._lock:
            asset_ids = list(self._subscribed)
        if asset_ids:
            await self._subscribe(asset_ids, replace=True)

    def _store_book(self, data):
        bids = []
        asks = []
        for level in data.get("bids", []) or []:
            try:
                bids.append({"price": float(level.get("price", 0.0)), "size": float(level.get("size", 0.0))})
            except Exception:
                continue
        for level in data.get("asks", []) or []:
            try:
                asks.append({"price": float(level.get("price", 0.0)), "size": float(level.get("size", 0.0))})
            except Exception:
                continue
        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])
        snapshot = {
            "market": data.get("market", ""),
            "asset_id": str(data.get("asset_id", "")),
            "timestamp": data.get("timestamp"),
            "bids": bids,
            "asks": asks,
            "received_at": time.time(),
        }
        with self._lock:
            self._books[snapshot["asset_id"]] = snapshot

    async def _run_forever(self):
        while self._running:
            if not await self._connect_ws():
                await asyncio.sleep(ORDERBOOK_WS_RECONNECT_SEC)
                continue
            try:
                await self._resubscribe_all()
                while self._running and self._ws is not None:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=25)
                    data = json.loads(raw)
                    messages = data if isinstance(data, list) else [data]
                    for item in messages:
                        if isinstance(item, dict) and item.get("event_type") == "book":
                            self._store_book(item)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning(f"   ⚠️ orderbook ws loop error: {e}")
            finally:
                self._connected = False
                if self._ws is not None:
                    try:
                        await self._ws.close()
                    except Exception:
                        pass
                self._ws = None
                if self._running:
                    await asyncio.sleep(ORDERBOOK_WS_RECONNECT_SEC)


def _weighted_fill_price(levels, contracts, fallback_price):
    """Средняя цена исполнения по стакану для заданного количества контрактов."""
    if not levels:
        return fallback_price, 0.0

    remaining = max(1, int(math.ceil(contracts)))
    cost = 0.0
    filled = 0.0

    for level in levels:
        try:
            price = float(level.get("price", 0))
            size = float(level.get("size", 0))
        except Exception:
            continue

        if price <= 0 or size <= 0:
            continue

        take = min(size, remaining)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    if filled <= 0:
        return fallback_price, 0.0

    avg_price = cost / filled
    slippage = max(0.0, avg_price - fallback_price)
    return avg_price, slippage


def get_executable_quote(token_id, contracts=1, fallback_price=0.0, market=None, outcome_name="Yes", live_book=None):
    """
    Возвращает исполнимые цены из стакана:
    - best_bid / best_ask
    - buy_price: средняя ask-цена для покупки нашего размера
    - sell_price: лучшая bid-цена для выхода
    """
    meta = get_market_metadata_quote(market or {}, outcome_name=outcome_name)
    book = live_book or fetch_clob_book(token_id)
    bids = book.get("bids", []) or []
    asks = book.get("asks", []) or []

    book_best_bid = _to_float(bids[0].get("price", 0), 0.0) if bids else 0.0
    book_best_ask = _to_float(asks[0].get("price", 0), 0.0) if asks else 0.0

    # Для weather exact markets market.bestBid/bestAsk отражают сторону YES корректнее,
    # а сырой /book иногда выглядит как инвертированная или пустая книга.
    best_bid = meta["best_bid"] or book_best_bid
    best_ask = meta["best_ask"] or book_best_ask

    buy_fallback = best_ask or meta["outcome_price"] or meta["last_trade"] or fallback_price or best_bid
    sell_fallback = best_bid or meta["outcome_price"] or meta["last_trade"] or fallback_price or best_ask
    book_buy_price, book_slippage = _weighted_fill_price(asks, contracts, buy_fallback)

    metadata_ask = meta["best_ask"] or meta["outcome_price"]
    if metadata_ask > 0:
        # Если стакан радикально расходится с market metadata, доверяем metadata.
        divergent = (
            book_buy_price <= 0
            or abs(book_buy_price - metadata_ask) > 0.25
            or (book_buy_price > 0.90 and metadata_ask < 0.10)
        )
        if divergent:
            buy_price = metadata_ask
            buy_slippage = max(0.0, buy_price - (meta["last_trade"] or fallback_price or buy_price))
        else:
            buy_price = book_buy_price
            buy_slippage = book_slippage
    else:
        buy_price = book_buy_price
        buy_slippage = book_slippage

    return {
        "book": book,
        "meta": meta,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "buy_price": buy_price,
        "buy_slippage": buy_slippage,
        "sell_price": sell_fallback,
    }

def place_bet(token_id, price, size_usd, client=None, side_label="YES"):
    """Размещает реальную ставку на выбранный outcome через Polymarket CLOB"""
    price = max(float(price), 0.0001)
    order_price = round(price, 4)
    contracts = max(1, round(size_usd / order_price))
    min_usd = float(POLY_MIN_ORDER_USD)
    bump_target = min_usd + max(0.0, POLY_MARKETABLE_BUFFER_USD)
    eps = 1e-12

    def _notional():
        return order_price * int(contracts)

    # Polymarket отклоняет marketable BUY если нотионал после усечения < $1 (в ответе API бывает ровно $0.999).
    def _notional_floor_millis():
        return math.floor(_notional() * 1000 + 1e-9) / 1000

    if POLY_AUTO_BUMP_TO_MIN:
        if contracts < POLY_MIN_CONTRACTS:
            contracts = POLY_MIN_CONTRACTS
        if _notional() < min_usd - eps:
            need = int(math.ceil(min_usd / order_price - eps))
            contracts = max(contracts, need, POLY_MIN_CONTRACTS)
        while _notional() < bump_target - eps:
            contracts += 1
        while _notional_floor_millis() < bump_target - eps:
            contracts += 1
    else:
        if _notional() < min_usd - eps:
            return {
                "status": "SKIP",
                "reason": f"Мало сумма (${_notional():.3f} по цене ордера), мин ${min_usd:.2f} (включите POLY_AUTO_BUMP_TO_MIN)",
            }

    cost = round(_notional(), 2)

    # Проверка минимумов Polymarket
    if contracts < POLY_MIN_CONTRACTS:
        return {"status": "SKIP", "reason": f"Мало контрактов ({contracts}), мин {POLY_MIN_CONTRACTS}"}

    req = bump_target if POLY_AUTO_BUMP_TO_MIN else min_usd
    if _notional() < req - eps or _notional_floor_millis() < req - eps:
        return {
            "status": "SKIP",
            "reason": (
                f"Мало сумма (${_notional():.3f}, floor3=${_notional_floor_millis():.3f}), "
                f"нужно ≥${req:.2f}"
            ),
        }

    if DRY_RUN:
        return {"status": "DRY_RUN", "cost": cost, "contracts": contracts, "side": side_label}
    
    if not client:
        return {"status": "ERROR", "reason": "Клиент не инициализирован"}

    def _to_float_local(value):
        try:
            if value in (None, ""):
                return 0.0
            return float(value)
        except Exception:
            return 0.0

    try:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        order = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=order_price, size=contracts, side=BUY)
        )
        order_data = order if isinstance(order, dict) else {}
        exchange_status = str(order_data.get("status", "") or "").strip().lower()
        order_id = (
            order_data.get("orderID")
            or order_data.get("orderId")
            or order_data.get("id")
        )
        matched_contracts = max(
            _to_float_local(order_data.get("size_matched")),
            _to_float_local(order_data.get("sizeMatched")),
            _to_float_local(order_data.get("filled_size")),
            _to_float_local(order_data.get("filledSize")),
        )

        if exchange_status == "matched" or matched_contracts >= contracts:
            return {
                "status": "FILLED",
                "order": order_data or str(order),
                "order_id": str(order_id) if order_id else None,
                "exchange_status": exchange_status or "matched",
                "cost": cost,
                "contracts": contracts,
                "matched_contracts": matched_contracts or contracts,
                "side": side_label,
            }

        return {
            "status": "POSTED",
            "order": order_data or str(order),
            "order_id": str(order_id) if order_id else None,
            "exchange_status": exchange_status or "live",
            "cost": cost,
            "contracts": contracts,
            "matched_contracts": matched_contracts,
            "side": side_label,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "ERROR", "reason": str(e)}


def _clob_price_quantize_4(price) -> float:
    """Цена CLOB с шагом 0.0001; float+round() давали 0.0 на крошечных bid — нельзя слать SELL по 0."""
    try:
        d = Decimal(str(float(price)))
    except (TypeError, ValueError, ArithmeticError):
        return 0.0
    q = d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(q)


def place_sell_order(token_id, price, contracts, client=None):
    """Размещает sell order YES по лучшему bid."""
    contracts = int(math.floor(float(contracts) + 1e-9))
    if contracts < 1:
        return {"status": "ERROR", "reason": "Размер SELL < 1 контракта"}
    px = _clob_price_quantize_4(price)
    if px <= 0:
        return {
            "status": "ERROR",
            "reason": (
                f"Цена SELL после квантования 4 знака ≤ 0 (исходный bid {price!r}) — "
                "слишком мелкий bid или ошибка float"
            ),
        }
    proceeds = round(px * contracts, 2)

    if DRY_RUN:
        return {"status": "DRY_RUN", "proceeds": proceeds, "contracts": contracts, "side": "SELL"}

    if not client:
        return {"status": "ERROR", "reason": "Клиент не инициализирован"}

    try:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import SELL

        order = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=px, size=contracts, side=SELL)
        )
        return {"status": "OK", "order": str(order), "proceeds": proceeds, "contracts": contracts, "side": "SELL"}
    except Exception as e:
        err_s = str(e).lower()
        if "not enough balance" in err_s or "allowance" in err_s:
            logger.warning(f"   ⚠️ place_sell_order: {e}")
        else:
            import traceback
            traceback.print_exc()
        return {"status": "ERROR", "reason": str(e)}

def send_telegram(message):
    """Отправляет уведомление в Telegram"""
    if not TG_ENABLED or not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"   📱 Telegram: отправлено")
            return True
        else:
            logger.warning(f"   ⚠️ Telegram error: {resp.status_code}")
            return False
    except Exception as e:
        logger.warning(f"   ⚠️ Telegram ошибка: {e}")
        return False


class WeatherBotV3:
    def __init__(self):
        self.api_url = "https://gamma-api.polymarket.com/events"
        self.data_url = "https://data-api.polymarket.com"
        self.client = None
        self.base_dir = os.path.dirname(__file__)
        self.instance_lock_file = os.path.join(self.base_dir, 'weather_bot.instance.lock')
        self.market_locks_dir = os.path.join(self.base_dir, 'market_locks')
        self.instance_lock_fd = None
        os.makedirs(self.market_locks_dir, exist_ok=True)
        self.acquire_instance_lock()
        atexit.register(self.release_instance_lock)
        self.session_budget = BANKROLL * SESSION_BUDGET_PCT
        self.gfs_history_file = os.path.join(self.base_dir, 'gfs_history.json')
        self.gfs_bias_file = os.path.join(self.base_dir, 'gfs_bias.json')  # GFS bias correction
        self.city_stoploss_file = os.path.join(self.base_dir, 'city_stoploss.json')
        self.purchases_file = os.path.join(self.base_dir, 'purchases.json')  # Постоянное хранилище
        self.pending_orders_file = os.path.join(self.base_dir, 'pending_orders.json')
        self.exit_actions_file = os.path.join(self.base_dir, 'exit_actions.json')
        self.telemetry_file = os.path.join(self.base_dir, 'trade_telemetry.jsonl')
        self.forecast_snapshot_file = os.path.join(self.base_dir, 'forecast_snapshots.jsonl')
        self.forecast_snapshot_index_file = os.path.join(self.base_dir, 'forecast_snapshot_index.json')
        self.forecast_learning_file = os.path.join(self.base_dir, 'forecast_learning_index.json')
        self.forecast_api_cache_file = os.path.join(self.base_dir, 'forecast_api_cache.json')
        self.city_loss_tracker = self.load_city_loss_tracker()
        self.gfs_bias = self.load_gfs_bias()  # GFS bias correction per city
        self.buy_counters = self.load_buy_counters()  # 🆕 Max 3 buys per market
        if AI_WEATHER:
            self.MAX_BUYS_PER_MARKET = int(os.getenv("MAX_BUYS_PER_MARKET_AI", "6"))
        else:
            self.MAX_BUYS_PER_MARKET = int(os.getenv("MAX_BUYS_PER_MARKET", "1"))
        self.exit_actions = self.load_exit_actions()
        self.forecast_snapshot_index = self.load_forecast_snapshot_index()
        self.forecast_learning_index = self.load_forecast_learning_index()
        self.forecast_api_cache = self.load_forecast_api_cache()
        self.book_cache = {}
        self.last_scan_summary = {}
        self._scan_min_hours_to_expiry = None
        self._last_auto_redeem_ts = None  # time.monotonic() при последней попытке AUTO_REDEEM
        self._last_auto_dump_dead_ts = None  # time.monotonic() при последней попытке AUTO_DUMP_DEAD
        self.open_meteo_backoff_until = None
        self.open_meteo_last_request_monotonic = 0.0
        self.pending_reprice_budget = {}
        self.pending_reprice_blocked = set()
        self.orderbook_ws = LiveOrderbookFeed()
        if ORDERBOOK_WS_ENABLED:
            self.orderbook_ws.start()
            atexit.register(self.orderbook_ws.stop)
        
        # 🛡️ LOCAL PURCHASE CACHE — предотвращает повторные покупки
        self.purchased_cache = set()  # {(city, temp, date_str)}
        self.purchased_token_cache = set()  # {token_id}
        self.pending_order_cache = set()  # {(city, temp, date_str)}
        self.pending_token_cache = set()  # {token_id}
        self.load_purchase_history()  # Загружаем из файла
        self.load_pending_orders()
        self.rebuild_buy_counters_from_purchases()
        
        # Инициализация ClobClient если DRY_RUN=false
        if not DRY_RUN and PRIVATE_KEY and FUNDER_ADDRESS:
            try:
                self.client = init_client(PRIVATE_KEY, FUNDER_ADDRESS)
                logger.info("   ✅ ClobClient инициализирован для реальных покупок")
            except Exception as e:
                logger.error(f"   ❌ Ошибка инициализации клиента: {e}")
                self.client = None

    def is_pid_running(self, pid):
        pid = int(pid)
        if pid <= 0:
            return False
        if sys.platform == 'win32':
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (result.stdout or "").strip()
                if not output:
                    return False
                rows = []
                try:
                    rows = list(csv.reader(output.splitlines()))
                except Exception:
                    rows = []
                for row in rows:
                    if len(row) >= 2 and str(row[1]).strip() == str(pid):
                        return True
                return False
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return False

    def acquire_instance_lock(self):
        payload = {
            'pid': os.getpid(),
            'started_at': datetime.now(timezone.utc).isoformat(),
            'dry_run': DRY_RUN,
        }
        try:
            self.instance_lock_fd = os.open(self.instance_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.instance_lock_fd, json.dumps(payload).encode('utf-8'))
            os.fsync(self.instance_lock_fd)
            logger.info(f"   🔒 Instance lock acquired (pid={os.getpid()})")
            return
        except FileExistsError:
            pass

        existing = {}
        try:
            with open(self.instance_lock_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except Exception:
            existing = {}

        existing_pid = existing.get('pid')
        if existing_pid and self.is_pid_running(existing_pid):
            raise RuntimeError(f"Уже запущен другой экземпляр бота (pid={existing_pid}). Остановите его перед новым запуском.")

        try:
            os.remove(self.instance_lock_file)
        except FileNotFoundError:
            pass
        self.instance_lock_fd = os.open(self.instance_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.instance_lock_fd, json.dumps(payload).encode('utf-8'))
        os.fsync(self.instance_lock_fd)
        logger.warning("   ⚠️ Найден stale instance lock, перехватываем его")

    def release_instance_lock(self):
        try:
            if self.instance_lock_fd is not None:
                os.close(self.instance_lock_fd)
                self.instance_lock_fd = None
        except Exception:
            pass
        try:
            if os.path.exists(self.instance_lock_file):
                with open(self.instance_lock_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('pid') == os.getpid():
                    os.remove(self.instance_lock_file)
        except Exception:
            pass

    def acquire_market_lock(self, purchase_key, token_id=None):
        city, temp, date_str = purchase_key
        token_suffix = re.sub(r'[^A-Za-z0-9_-]+', '_', str(token_id or 'no_token'))
        file_name = f"{city}_{temp}_{date_str}_{token_suffix}.lock"
        path = os.path.join(self.market_locks_dir, file_name)
        payload = {
            'pid': os.getpid(),
            'purchase_key': [city, temp, date_str],
            'token_id': str(token_id) if token_id else None,
            'ts': datetime.now(timezone.utc).isoformat(),
        }
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps(payload).encode('utf-8'))
            os.close(fd)
            return path
        except FileExistsError:
            existing = {}
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                existing = {}
            existing_pid = existing.get('pid')
            if existing_pid and self.is_pid_running(existing_pid):
                return None
            try:
                os.remove(path)
            except FileNotFoundError:
                return None
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps(payload).encode('utf-8'))
            os.close(fd)
            logger.warning(f"   ⚠️ Перехватываем stale market lock для {city} {temp}°C {date_str}")
            return path

    def release_market_lock(self, lock_path):
        if not lock_path:
            return
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

    def load_exit_actions(self):
        """Хранит уже выполненные/смоделированные exit-действия, чтобы не спамить один и тот же partial sell."""
        if os.path.exists(self.exit_actions_file):
            try:
                with open(self.exit_actions_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки exit_actions: {e}")
        return {}

    def save_exit_actions(self):
        try:
            with open(self.exit_actions_file, 'w') as f:
                json.dump(self.exit_actions, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось сохранить exit_actions: {e}")

    def mark_exit_action(self, token_id, action):
        if not token_id:
            return
        key = str(token_id)
        actions = set(self.exit_actions.get(key, []))
        actions.add(action)
        self.exit_actions[key] = sorted(actions)
        self.save_exit_actions()

    def has_exit_action(self, token_id, action):
        if not token_id:
            return False
        return action in set(self.exit_actions.get(str(token_id), []))

    def get_live_orderbook_snapshot(self, token_id):
        if not token_id or not ORDERBOOK_WS_ENABLED:
            return None
        try:
            return self.orderbook_ws.get_book(str(token_id))
        except Exception:
            return None

    def get_cached_quote(self, token_id, contracts=1, fallback_price=0.0, market=None, outcome_name="Yes"):
        if token_id and ORDERBOOK_WS_ENABLED:
            self.orderbook_ws.ensure_assets([token_id])
        live_book = self.get_live_orderbook_snapshot(token_id)
        cache_key = (
            str(token_id),
            str(outcome_name or "Yes").lower(),
            max(1, int(math.ceil(contracts))),
            round(float(fallback_price or 0.0), 4),
            round(_to_float((market or {}).get("bestBid"), 0.0), 4),
            round(_to_float((market or {}).get("bestAsk"), 0.0), 4),
            round(_to_float(((live_book or {}).get("bids") or [{}])[0].get("price", 0.0) if (live_book or {}).get("bids") else 0.0, 0.0), 4),
            round(_to_float(((live_book or {}).get("asks") or [{}])[0].get("price", 0.0) if (live_book or {}).get("asks") else 0.0, 0.0), 4),
        )
        if cache_key not in self.book_cache:
            self.book_cache[cache_key] = get_executable_quote(
                token_id,
                contracts=contracts,
                fallback_price=fallback_price,
                market=market,
                outcome_name=outcome_name,
                live_book=live_book,
            )
        return self.book_cache[cache_key]

    def write_telemetry(self, event_type, payload):
        record = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event_type,
            'dry_run': DRY_RUN,
        }
        record.update(payload or {})
        try:
            with open(self.telemetry_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"   ⚠️ telemetry write failed: {e}")

    def load_city_loss_tracker(self):
        """Загружает трекер поражений городов (для stop-loss)"""
        if os.path.exists(self.city_stoploss_file):
            try:
                with open(self.city_stoploss_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}  # {city: {'losses': N, 'blocked_until': 'YYYY-MM-DD'}}

    def load_purchase_history(self):
        """Загружает историю покупок из файла (постоянное хранилище)"""
        if os.path.exists(self.purchases_file):
            try:
                with open(self.purchases_file, 'r') as f:
                    data = json.load(f)

                # 🔧 v4.0.3: Удаляем дубликаты (city+temp+date)
                seen = set()
                unique_data = []
                for item in data:
                    key = (item.get('city', '').lower(),
                           int(item.get('temp', 0)),
                           item.get('date', '')[:10])
                    if key not in seen:
                        seen.add(key)
                        unique_data.append(item)

                if len(unique_data) < len(data):
                    logger.warning(f"   ⚠️ Найдено {len(data) - len(unique_data)} дубликатов в purchases.json — удалено")
                    with open(self.purchases_file, 'w') as f:
                        json.dump(unique_data, f, indent=2, ensure_ascii=False)
                    data = unique_data

                # Восстанавливаем cache
                for item in data:
                    city = item.get('city', '').lower()
                    temp = int(item.get('temp', 0))
                    date = item.get('date', '')[:10]
                    token_id = str(item.get('token_id', '') or '')
                    if city and date:
                        self.purchased_cache.add((city, temp, date))
                    if token_id:
                        self.purchased_token_cache.add(token_id)
                logger.info(f"   📂 Загружено {len(self.purchased_cache)} уникальных покупок из файла")
            except Exception as e:
                # ❌ НИКОГДА не удаляем файл! Только предупреждаем
                logger.warning(f"   ⚠️ Ошибка загрузки покупок: {e}")
                logger.warning(f"   ⚠️ purchases.json НЕ удалён — проверяем при следующем запуске")

    def rebuild_pending_order_cache(self, data):
        self.pending_order_cache = set()
        self.pending_token_cache = set()
        for item in data or []:
            status = str(item.get('status', 'posted') or 'posted').lower()
            if status not in {'posted', 'live', 'unmatched', 'delayed', 'pending'}:
                continue
            city = item.get('city', '').lower()
            date = item.get('date', '')[:10]
            token_id = str(item.get('token_id', '') or '')
            try:
                temp = int(item.get('temp', 0))
            except Exception:
                continue
            if city and date:
                self.pending_order_cache.add((city, temp, date))
            if token_id:
                self.pending_token_cache.add(token_id)

    def load_pending_orders(self):
        if not os.path.exists(self.pending_orders_file):
            return
        try:
            with open(self.pending_orders_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
            self.rebuild_pending_order_cache(data)
            if self.pending_order_cache:
                logger.info(f"   📌 Загружено {len(self.pending_order_cache)} pending orders")
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка загрузки pending orders: {e}")

    def read_pending_orders_data(self):
        if not os.path.exists(self.pending_orders_file):
            return []
        try:
            with open(self.pending_orders_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось прочитать pending orders: {e}")
            return []

    def write_pending_orders_data(self, data):
        try:
            with open(self.pending_orders_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            self.rebuild_pending_order_cache(data)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось записать pending orders: {e}")

    def parse_utc_timestamp(self, value):
        if not value:
            return None
        try:
            text = str(value).strip()
            if not text:
                return None
            return datetime.fromisoformat(text.replace('Z', '+00:00'))
        except Exception:
            return None

    def save_pending_order(self, city, temp, date_str, token_id=None, order_id=None, side=None, price=None, contracts=None, exchange_status=None, reprice_count=None, aggressive=None, model_prob=None, edge=None, confidence=None, risk_tier=None, is_tail=None, is_no_hedge=None, signal_score=None):
        try:
            data = self.read_pending_orders_data()

            city_lower = str(city or '').lower()
            temp_int = int(temp)
            date_key = str(date_str)[:10]
            token_str = str(token_id) if token_id else None
            order_str = str(order_id) if order_id else None
            now_iso = datetime.now(timezone.utc).isoformat()

            for item in data:
                same_exact_market = (
                    item.get('city', '').lower() == city_lower
                    and int(item.get('temp', 0)) == temp_int
                    and item.get('date', '')[:10] == date_key
                )
                same_token = token_str and str(item.get('token_id', '') or '') == token_str
                same_order = order_str and str(item.get('order_id', '') or '') == order_str
                if same_exact_market or same_token or same_order:
                    item.update({
                        'status': str(exchange_status or item.get('status') or 'posted').lower(),
                        'order_id': order_str or item.get('order_id'),
                        'token_id': token_str or item.get('token_id'),
                        'side': side or item.get('side'),
                        'price': price if price is not None else item.get('price'),
                        'contracts': contracts if contracts is not None else item.get('contracts'),
                        'updated_at': now_iso,
                        'reprice_count': int(reprice_count if reprice_count is not None else item.get('reprice_count', 0) or 0),
                        'aggressive': bool(item.get('aggressive')) if aggressive is None else bool(aggressive),
                        'model_prob': model_prob if model_prob is not None else item.get('model_prob'),
                        'edge': edge if edge is not None else item.get('edge'),
                        'confidence': confidence if confidence is not None else item.get('confidence'),
                        'risk_tier': risk_tier if risk_tier is not None else item.get('risk_tier'),
                        'is_tail': bool(item.get('is_tail')) if is_tail is None else bool(is_tail),
                        'is_no_hedge': bool(item.get('is_no_hedge')) if is_no_hedge is None else bool(is_no_hedge),
                        'signal_score': signal_score if signal_score is not None else item.get('signal_score'),
                        'first_posted_at': item.get('first_posted_at') or item.get('timestamp') or now_iso,
                    })
                    self.write_pending_orders_data(data)
                    return

            data.append({
                'city': city_lower,
                'temp': temp_int,
                'date': date_key,
                'token_id': token_str,
                'order_id': order_str,
                'side': side,
                'price': price,
                'contracts': contracts,
                'status': str(exchange_status or 'posted').lower(),
                'timestamp': now_iso,
                'updated_at': now_iso,
                'first_posted_at': now_iso,
                'reprice_count': int(reprice_count or 0),
                'aggressive': bool(aggressive),
                'model_prob': model_prob,
                'edge': edge,
                'confidence': confidence,
                'risk_tier': risk_tier,
                'is_tail': bool(is_tail),
                'is_no_hedge': bool(is_no_hedge),
                'signal_score': signal_score,
            })
            self.write_pending_orders_data(data)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось сохранить pending order: {e}")

    def clear_pending_order(self, purchase_key=None, token_id=None, order_id=None):
        try:
            if not os.path.exists(self.pending_orders_file):
                return
            with open(self.pending_orders_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []

            filtered = []
            for item in data:
                item_key = (
                    item.get('city', '').lower(),
                    int(item.get('temp', 0)),
                    item.get('date', '')[:10],
                )
                item_token = str(item.get('token_id', '') or '')
                item_order = str(item.get('order_id', '') or '')
                if purchase_key and item_key == purchase_key:
                    continue
                if token_id and item_token == str(token_id):
                    continue
                if order_id and item_order == str(order_id):
                    continue
                filtered.append(item)

            self.write_pending_orders_data(filtered)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось очистить pending order: {e}")

    def sync_pending_orders_from_api(self):
        if DRY_RUN or self.client is None or not os.path.exists(self.pending_orders_file):
            return
        try:
            with open(self.pending_orders_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list) or not data:
                self.rebuild_pending_order_cache([])
                return

            open_orders = self.client.get_orders()
            open_order_ids = set()
            for order in open_orders or []:
                order_id = order.get('orderID') or order.get('orderId') or order.get('id')
                if order_id:
                    open_order_ids.add(str(order_id))

            filtered = []
            dropped = 0
            for item in data:
                item_key = (
                    item.get('city', '').lower(),
                    int(item.get('temp', 0)),
                    item.get('date', '')[:10],
                )
                item_token = str(item.get('token_id', '') or '')
                item_order = str(item.get('order_id', '') or '')
                if item_key in self.purchased_cache or (item_token and item_token in self.purchased_token_cache):
                    dropped += 1
                    continue
                if item_order and item_order not in open_order_ids:
                    dropped += 1
                    continue
                filtered.append(item)

            if dropped:
                logger.info(f"   🧹 Очистили {dropped} устаревших pending orders")
            self.write_pending_orders_data(filtered)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось sync pending orders: {e}")

    def cancel_open_order(self, order_id, reason="stale_pending"):
        if DRY_RUN or self.client is None or not order_id:
            return False
        try:
            self.client.cancel(str(order_id))
            logger.info(f"   🧹 Cancelled order {order_id} ({reason})")
            return True
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось отменить order {order_id}: {e}")
            return False

    def manage_pending_orders(self):
        self.pending_reprice_budget = {}
        self.pending_reprice_blocked = set()
        data = self.read_pending_orders_data()
        if not data:
            return

        now = datetime.now(timezone.utc)
        filtered = []
        changed = False
        stale_cleared = 0

        for item in data:
            status = str(item.get('status', 'posted') or 'posted').lower()
            city = item.get('city', '').lower()
            date_key = item.get('date', '')[:10]
            token_id = str(item.get('token_id', '') or '')
            order_id = str(item.get('order_id', '') or '')
            try:
                temp = int(item.get('temp', 0))
            except Exception:
                temp = 0
            purchase_key = (city, temp, date_key)

            if purchase_key in self.purchased_cache or (token_id and token_id in self.purchased_token_cache):
                changed = True
                continue

            if status not in {'posted', 'live', 'unmatched', 'delayed', 'pending'}:
                filtered.append(item)
                continue

            ts = self.parse_utc_timestamp(item.get('updated_at') or item.get('timestamp') or item.get('first_posted_at'))
            age_sec = (now - ts).total_seconds() if ts else None
            ttl_sec = PENDING_ORDER_TTL_SEC if order_id else PENDING_ORPHAN_TTL_SEC
            if age_sec is None or age_sec < ttl_sec:
                filtered.append(item)
                continue

            reprice_count = int(item.get('reprice_count', 0) or 0)
            if reprice_count >= MAX_PENDING_REPRICES:
                logger.info(f"   ⏭️ Pending {city.upper()} {temp}°C достиг лимита reprices ({reprice_count})")
                self.pending_reprice_blocked.add(purchase_key)
                changed = True
                self.write_telemetry("pending_drop", {
                    'city': city,
                    'temp': temp,
                    'date': date_key,
                    'reason': 'reprice_limit_reached',
                    'order_id': order_id or None,
                    'reprice_count': reprice_count,
                })
                continue

            if order_id and not self.cancel_open_order(order_id, reason="stale_reprice"):
                filtered.append(item)
                continue

            next_reprice_count = reprice_count + 1
            self.pending_reprice_budget[purchase_key] = next_reprice_count
            changed = True
            stale_cleared += 1
            self.write_telemetry("pending_reprice_ready", {
                'city': city,
                'temp': temp,
                'date': date_key,
                'order_id': order_id or None,
                'reprice_count': next_reprice_count,
                'age_sec': round(age_sec, 1) if age_sec is not None else None,
            })

        if changed:
            self.write_pending_orders_data(filtered)
        if stale_cleared:
            logger.info(f"   🔁 Pending execution refresh: {stale_cleared} orders marked for repricing")

    def save_purchase(self, city, temp, date_str, token_id=None):
        """Сохраняет покупку в постоянное хранилище"""
        try:
            data = []
            if os.path.exists(self.purchases_file):
                with open(self.purchases_file, 'r') as f:
                    data = json.load(f)

            city_lower = city.lower()
            temp_int = int(temp)
            date_key = str(date_str)[:10]
            token_str = str(token_id) if token_id else None

            for item in data:
                same_exact_market = (
                    item.get('city', '').lower() == city_lower
                    and int(item.get('temp', 0)) == temp_int
                    and item.get('date', '')[:10] == date_key
                )
                same_token = token_str and str(item.get('token_id', '') or '') == token_str
                if same_exact_market or same_token:
                    return

            data.append({
                'city': city_lower,
                'temp': temp_int,  # Конвертируем np.int64 → int
                'date': date_key,
                'token_id': token_str,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            
            with open(self.purchases_file, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось сохранить покупку: {e}")

    def clear_exit_actions_for_token(self, token_id):
        if not token_id:
            return
        key = str(token_id)
        if key in self.exit_actions:
            del self.exit_actions[key]
            self.save_exit_actions()
            logger.info(f"   🧹 Сброшены exit_actions для token {key[:16]}…")

    def remove_purchase_record(self, city, temp, date_str, token_id=None):
        """
        Убирает ключ из cache / purchases.json после полного выхода или reconcile.
        Позволяет снова купить тот же рынок, если появится сигнал.
        """
        try:
            city_lower = (city or "").lower()
            temp_int = int(round(temp))
            date_key = str(date_str)[:10]
            token_str = str(token_id) if token_id else ""

            self.purchased_cache.discard((city_lower, temp_int, date_key))
            if token_str:
                self.purchased_token_cache.discard(token_str)

            if not os.path.exists(self.purchases_file):
                if token_str:
                    self.clear_exit_actions_for_token(token_str)
                self.rebuild_buy_counters_from_purchases()
                return

            with open(self.purchases_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            new_data = []
            removed = False
            for item in data:
                same_key = (
                    item.get("city", "").lower() == city_lower
                    and int(item.get("temp", 0)) == temp_int
                    and item.get("date", "")[:10] == date_key
                )
                same_tok = token_str and str(item.get("token_id", "") or "") == token_str
                if same_key or same_tok:
                    itok = str(item.get("token_id", "") or "")
                    if itok:
                        self.purchased_token_cache.discard(itok)
                        self.clear_exit_actions_for_token(itok)
                    removed = True
                    continue
                new_data.append(item)

            if removed:
                with open(self.purchases_file, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, indent=2, ensure_ascii=False, default=str)
                logger.info(
                    f"   🧹 Удалена запись покупки: {city_lower.upper()} "
                    f"{format_purchase_temp_label(temp_int)} {date_key}"
                )
                self.rebuild_buy_counters_from_purchases()
        except Exception as e:
            logger.warning(f"   ⚠️ remove_purchase_record: {e}")

    def reconcile_purchases_with_positions(self, all_positions):
        """
        Удаляет строки purchases.json, если token_id больше нет среди открытых позиций (size>0).
        Не трогает записи без token_id. Не трогает токены из pending (ордер ещё не в позициях).

        Безопасные случаи (автоочистка проданного): есть хотя бы одна открытая позиция по API, либо API
        вернул непустой список (даже если везде size≈0 — считаем ответ достоверным). Риск «дублей» только
        если API вернул [] при живых позициях — тогда нужен RECONCILE_TRUST_EMPTY_POSITIONS_API.
        """
        if not RECONCILE_PURCHASES_WITH_API:
            logger.debug(
                "Reconcile purchases↔API выключен (RECONCILE_PURCHASES_WITH_API=false)"
            )
            return
        if all_positions is None:
            logger.warning(
                "   🛡️ Reconcile пропущен: позиции API недоступны (None) — не трогаем purchases.json"
            )
            return

        pos_list = all_positions or []
        n_pos = len(pos_list)
        active_tokens = set()
        for pos in pos_list:
            tid = str(pos.get("asset") or pos.get("asset_id") or pos.get("token_id") or "")
            try:
                sz = float(pos.get("size", 0) or 0)
            except Exception:
                sz = 0.0
            if tid and sz > 1e-9:
                active_tokens.add(tid)

        if not os.path.exists(self.purchases_file):
            return
        try:
            with open(self.purchases_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            rows_with_token = sum(1 for item in data if str(item.get("token_id", "") or "").strip())
            # Можно чистить «проданное», если API явно «живой»: есть открытые size>0 ИЛИ пришёл непустой список позиций.
            # Только [] + строки в файле — подозрительно (сбой API); без TRUST не трогаем.
            api_nonempty = n_pos > 0
            safe_to_reconcile = (
                len(active_tokens) >= 1
                or api_nonempty
                or (rows_with_token == 0)
            )
            if not safe_to_reconcile and rows_with_token > 0:
                if RECONCILE_TRUST_EMPTY_POSITIONS_API:
                    safe_to_reconcile = True
                else:
                    logger.warning(
                        "   🛡️ Reconcile пропущен: API вернул пустой список позиций [], "
                        f"в purchases.json есть {rows_with_token} строк с token_id — "
                        "без RECONCILE_TRUST_EMPTY_POSITIONS_API=true не удаляем (защита от дублей)"
                    )
                    return

            new_data = []
            removed_any = False
            for item in data:
                tid = str(item.get("token_id", "") or "")
                if not tid:
                    new_data.append(item)
                    continue
                if tid in self.pending_token_cache:
                    new_data.append(item)
                    continue
                if tid not in active_tokens:
                    city = item.get("city", "").lower()
                    try:
                        temp = int(item.get("temp", 0))
                    except Exception:
                        temp = 0
                    d = item.get("date", "")[:10]
                    self.purchased_cache.discard((city, temp, d))
                    self.purchased_token_cache.discard(tid)
                    self.clear_exit_actions_for_token(tid)
                    removed_any = True
                    logger.info(
                        f"   🧹 Reconcile: закрытая позиция убрана из purchases — "
                        f"{city} {format_purchase_temp_label(temp)} {d}"
                    )
                    continue
                new_data.append(item)

            if removed_any:
                with open(self.purchases_file, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, indent=2, ensure_ascii=False, default=str)
                self.rebuild_buy_counters_from_purchases()
        except Exception as e:
            logger.warning(f"   ⚠️ reconcile_purchases_with_positions: {e}")

    def rebuild_buy_counters_from_purchases(self):
        """Восстанавливает exact-market counters из purchases.json, чтобы старый формат city|date не ослаблял защиту."""
        rebuilt = {}
        try:
            if os.path.exists(self.purchases_file):
                with open(self.purchases_file, 'r') as f:
                    data = json.load(f)
                for item in data:
                    city = item.get('city', '').lower()
                    date = item.get('date', '')[:10]
                    try:
                        temp = int(item.get('temp', 0))
                    except Exception:
                        continue
                    if city and date:
                        key = (city, temp, date)
                        rebuilt[key] = max(1, rebuilt.get(key, 0))
        except Exception as e:
            logger.warning(f"   ⚠️ Не удалось rebuild buy counters: {e}")
            return

        if rebuilt != self.buy_counters:
            self.buy_counters = rebuilt
            self.save_buy_counters()
            logger.info(f"   🛡️ Rebuilt exact-market buy counters: {self.buy_counters}")

    def save_city_loss_tracker(self):
        """Сохраняет трекер поражений городов"""
        with open(self.city_stoploss_file, 'w') as f:
            json.dump(self.city_loss_tracker, f, indent=2, ensure_ascii=False)

    def load_gfs_bias(self):
        """
        Загружает GFS bias данные из файла.
        Поддерживает старый формат city-level и новый формат city + lead bucket.
        bias = GFS_прогноз - реальность
        bias < 0 → GFS занижает (реальность теплее)
        bias > 0 → GFS завышает (реальность холоднее)
        """
        if os.path.exists(self.gfs_bias_file):
            try:
                with open(self.gfs_bias_file, 'r') as f:
                    raw = json.load(f)
                migrated = {}
                for city, value in (raw or {}).items():
                    if isinstance(value, dict) and 'overall' in value:
                        migrated[city] = value
                        continue
                    errors = list((value or {}).get('errors', []))
                    bias = float((value or {}).get('bias', 0.0))
                    count = int((value or {}).get('count', len(errors)))
                    mae = float((value or {}).get('mae', (sum(abs(e) for e in errors) / len(errors)) if errors else abs(bias)))
                    migrated[city] = {
                        "overall": {"errors": errors, "bias": bias, "mae": mae, "count": count},
                        "by_lead": {}
                    }
                return migrated
            except:
                pass
        return {}

    def save_gfs_bias(self):
        """Сохраняет GFS bias данные"""
        with open(self.gfs_bias_file, 'w') as f:
            json.dump(self.gfs_bias, f, indent=2, ensure_ascii=False)

    def load_forecast_snapshot_index(self):
        if os.path.exists(self.forecast_snapshot_index_file):
            try:
                with open(self.forecast_snapshot_index_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки forecast snapshot index: {e}")
        return {}

    def save_forecast_snapshot_index(self):
        try:
            with open(self.forecast_snapshot_index_file, 'w', encoding='utf-8') as f:
                json.dump(self.forecast_snapshot_index, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка сохранения forecast snapshot index: {e}")

    def load_forecast_learning_index(self):
        if os.path.exists(self.forecast_learning_file):
            try:
                with open(self.forecast_learning_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки forecast learning index: {e}")
        return {}

    def save_forecast_learning_index(self):
        try:
            with open(self.forecast_learning_file, 'w', encoding='utf-8') as f:
                json.dump(self.forecast_learning_index, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка сохранения forecast learning index: {e}")

    def load_forecast_api_cache(self):
        if os.path.exists(self.forecast_api_cache_file):
            try:
                with open(self.forecast_api_cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки forecast API cache: {e}")
        return {}

    def save_forecast_api_cache(self):
        try:
            with open(self.forecast_api_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.forecast_api_cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка сохранения forecast API cache: {e}")

    def get_forecast_refresh_interval(self, event_date):
        if event_date is None:
            return 1800
        hours_to_resolution = max(0.0, (event_date - datetime.now(timezone.utc)).total_seconds() / 3600.0)
        if hours_to_resolution <= 12:
            return 900
        if hours_to_resolution <= 24:
            return 1200
        if hours_to_resolution <= 48:
            return 1800
        if hours_to_resolution <= 72:
            return 2700
        return 3600

    def get_cached_real_forecast(self, city, event_date_str, max_age_sec=None):
        cache_key = f"{(city or '').lower()}|{str(event_date_str)[:10]}"
        cached = self.forecast_api_cache.get(cache_key)
        if not cached:
            return None
        fetched_at = cached.get('fetched_at')
        if not fetched_at:
            return None
        try:
            fetched_dt = datetime.fromisoformat(str(fetched_at).replace('Z', '+00:00'))
            if fetched_dt.tzinfo is None:
                fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

        age_sec = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
        age_limit = FORECAST_CACHE_TTL_SEC if max_age_sec is None else min(FORECAST_CACHE_TTL_SEC, float(max_age_sec))
        if age_sec > age_limit:
            return None

        forecast = dict(cached.get('forecast') or {})
        if not forecast:
            return None

        cache_penalty = 0.08 if age_sec > 1800 else 0.04
        forecast['source'] = 'ensemble_cache'
        forecast['cache_age_sec'] = round(age_sec, 1)
        forecast['confidence'] = max(0.0, min(1.0, float(forecast.get('confidence', 0.5)) - cache_penalty))
        profile = self.get_confidence_profile(city, forecast['confidence'])
        forecast['risk_tier'] = profile['risk_tier']
        forecast['size_multiplier'] = profile['size_multiplier']
        forecast['tails_allowed'] = profile['tails_allowed']
        forecast['should_skip'] = profile['should_skip']
        return forecast

    def store_cached_real_forecast(self, city, event_date_str, forecast_result):
        cache_key = f"{(city or '').lower()}|{str(event_date_str)[:10]}"
        self.forecast_api_cache[cache_key] = {
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'forecast': forecast_result,
        }
        self.save_forecast_api_cache()

    def get_lead_bucket(self, hours_to_resolution):
        if hours_to_resolution is None:
            return "unknown"
        if hours_to_resolution <= 24:
            return "0_24h"
        if hours_to_resolution <= 48:
            return "24_48h"
        if hours_to_resolution <= 72:
            return "48_72h"
        return "72h_plus"

    def get_bias_stats(self, city, hours_to_resolution=None):
        city_lower = (city or "").lower()
        city_data = self.gfs_bias.get(city_lower, {})
        overall = city_data.get("overall", {})
        lead_bucket = self.get_lead_bucket(hours_to_resolution)
        bucket_data = (city_data.get("by_lead") or {}).get(lead_bucket, {})
        if bucket_data.get("count", 0) >= MIN_BIAS_BUCKET_COUNT:
            return bucket_data, lead_bucket, "bucket"
        return overall, lead_bucket, "overall"

    def save_forecast_snapshot(self, city, event_date_str, forecast_data):
        city_lower = (city or "").lower()
        event_date = str(event_date_str)[:10]
        key = f"{city_lower}|{event_date}"
        previous = self.forecast_snapshot_index.get(key)
        revision_peak = None
        revision_mean = None
        if previous:
            try:
                revision_peak = abs(float(forecast_data.get('peak', 0)) - float(previous.get('peak', 0)))
                revision_mean = abs(float(forecast_data.get('forecast_max', 0)) - float(previous.get('forecast_max', 0)))
            except Exception:
                revision_peak = None
                revision_mean = None

        snapshot = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'city': city_lower,
            'event_date': event_date,
            'peak': int(round(forecast_data.get('peak', 0))),
            'forecast_max': round(float(forecast_data.get('forecast_max', 0.0)), 3),
            'forecast_median': round(float(forecast_data.get('forecast_median', forecast_data.get('peak', 0.0))), 3),
            'spread': round(float(forecast_data.get('spread', 0.0)), 3),
            'sigma': round(float(forecast_data.get('sigma', GFS_STD)), 3),
            'confidence': round(float(forecast_data.get('confidence', 0.0)), 3),
            'risk_tier': forecast_data.get('risk_tier'),
            'hours_to_resolution': round(float(forecast_data.get('hours_to_resolution', 0.0)), 3),
            'lead_bucket': self.get_lead_bucket(forecast_data.get('hours_to_resolution')),
            'revision_peak': revision_peak,
            'revision_mean': revision_mean,
            'models': forecast_data.get('models', {}),
            'model_weights': forecast_data.get('model_weights', {}),
        }

        try:
            with open(self.forecast_snapshot_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(snapshot, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"   ⚠️ Ошибка записи forecast snapshot: {e}")

        self.forecast_snapshot_index[key] = snapshot
        self.save_forecast_snapshot_index()
        return snapshot

    # ═══════════════════════════════════════════════════════════════
    # 🆕 BUY COUNTERS — exact market only (city + temp + date)
    # ═══════════════════════════════════════════════════════════════

    BUY_COUNTERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'buy_counters.json')
    MAX_BUYS_PER_MARKET = 1  # Один exact market покупаем максимум один раз

    def load_buy_counters(self):
        """
        Загружает счётчик покупок по рынкам.
        Новый формат: {("toronto", -3, "2026-04-07"): 1}
        Старый формат city|date игнорируем, потому что он не различает температуру.
        """
        filepath = self.BUY_COUNTERS_FILE
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                # Конвертируем ключи из строк обратно в кортежи
                counters = {}
                for key_str, count in data.items():
                    parts = key_str.split('|')
                    if len(parts) == 3:
                        city, temp, date = parts
                        counters[(city, int(temp), date)] = int(count)
                logger.info(f"   🆕 Buy counters загружен: {counters}")
                return counters
            except Exception as e:
                logger.warning(f"   ⚠️ Ошибка загрузки buy counters: {e}")
        return {}

    def save_buy_counters(self):
        """Сохраняет счётчик покупок"""
        # Конвертируем кортежи в строки для JSON
        data = {f"{city}|{temp}|{date}": count for (city, temp, date), count in self.buy_counters.items()}
        with open(self.BUY_COUNTERS_FILE, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def check_buy_limit(self, city, temp, date_str):
        """
        Проверяет лимит покупок на рынок.
        Возвращает (allowed: bool, current_count: int)
        """
        key = (city.lower(), int(temp), date_str[:10])
        current = self.buy_counters.get(key, 0)

        if current >= self.MAX_BUYS_PER_MARKET:
            return False, current

        return True, current

    def increment_buy_counter(self, city, temp, date_str):
        """Увеличивает счётчик покупок"""
        key = (city.lower(), int(temp), date_str[:10])
        self.buy_counters[key] = self.buy_counters.get(key, 0) + 1
        self.save_buy_counters()
        logger.info(
            f"   🆕 Buy counter: {key[0].upper()} {format_purchase_temp_label(key[1])} {key[2]} → "
            f"{self.buy_counters[key]}/{self.MAX_BUYS_PER_MARKET}"
        )

    def apply_bias_correction(self, city, gfs_peak, hours_to_resolution=None):
        """
        Применяет bias correction к GFS прогнозу.
        Возвращает corrected_peak и информацию о bias.
        """
        bias_data, lead_bucket, bias_scope = self.get_bias_stats(city, hours_to_resolution)
        if not bias_data or bias_data.get('count', 0) < MIN_BIAS_BUCKET_COUNT:
            # Недостаточно данных — возвращаем как есть
            return gfs_peak, None

        bias = float(bias_data.get('bias', 0))
        count = int(bias_data.get('count', 0))
        mae = float(bias_data.get('mae', abs(bias)))

        # corrected = GFS - bias
        # bias=-1.5 (GFS занижает) → corrected = GFS + 1.5
        corrected = round(gfs_peak - bias)

        logger.info(
            f"   🔧 Bias correction: {city} GFS={gfs_peak}°C, bias={bias:+.2f} "
            f"(scope={bias_scope}, bucket={lead_bucket}, mae={mae:.2f}, n={count}) → corrected={corrected}°C"
        )
        return corrected, {
            'raw': gfs_peak,
            'corrected': corrected,
            'bias': bias,
            'mae': mae,
            'count': count,
            'lead_bucket': lead_bucket,
            'scope': bias_scope,
        }

    def record_gfs_error(self, city, gfs_predicted, actual_temp, hours_to_resolution=None):
        """
        Записывает ошибку GFS после экспирации.
        error = GFS_прогноз - реальность
        """
        city_lower = city.lower()
        error = gfs_predicted - actual_temp

        if city_lower not in self.gfs_bias:
            self.gfs_bias[city_lower] = {"overall": {"errors": [], "bias": 0, "mae": 0, "count": 0}, "by_lead": {}}

        city_data = self.gfs_bias[city_lower]
        lead_bucket = self.get_lead_bucket(hours_to_resolution)

        if "overall" not in city_data:
            city_data["overall"] = {"errors": [], "bias": 0, "mae": 0, "count": 0}
        if "by_lead" not in city_data:
            city_data["by_lead"] = {}
        if lead_bucket not in city_data["by_lead"]:
            city_data["by_lead"][lead_bucket] = {"errors": [], "bias": 0, "mae": 0, "count": 0}

        for bucket_data in (city_data["overall"], city_data["by_lead"][lead_bucket]):
            bucket_data["errors"].append(error)
            if len(bucket_data["errors"]) > MAX_BIAS_ERRORS_PER_BUCKET:
                bucket_data["errors"] = bucket_data["errors"][-MAX_BIAS_ERRORS_PER_BUCKET:]
            bucket_data["bias"] = sum(bucket_data["errors"]) / len(bucket_data["errors"])
            bucket_data["mae"] = sum(abs(x) for x in bucket_data["errors"]) / len(bucket_data["errors"])
            bucket_data["count"] = len(bucket_data["errors"])

        self.save_gfs_bias()
        logger.info(
            f"   📝 GFS error recorded: {city} predicted={gfs_predicted}°C, actual={actual_temp}°C, "
            f"error={error:+.1f}, bucket={lead_bucket}, overall_bias={city_data['overall']['bias']:+.2f}"
        )

    def is_exact_temperature_market(self, title):
        title_lower = (title or "").lower()
        return "or below" not in title_lower and "or higher" not in title_lower and "or above" not in title_lower

    def classify_bucket_market(self, question):
        """('higher'|'below', threshold_c) или None — рынки or higher / or above / or below / or lower."""
        if not question:
            return None
        tl = question.lower()
        # Fahrenheit-бакеты: порог в °F, модель в °C — пока не торгуем (ложные ключи в cache).
        if "°f" in tl or "fahrenheit" in tl:
            return None
        if "or higher" in tl or "or above" in tl:
            kind = "higher"
        elif "or below" in tl or "or lower" in tl:
            kind = "below"
        else:
            return None
        th = self._parse_threshold_c_from_question(question)
        if th is None:
            return None
        return (kind, th)

    def _parse_threshold_c_from_question(self, question):
        ql = (question or "").lower()
        m = re.search(r"(-?\d+)\s*°?\s*c", ql)
        if m:
            return int(m.group(1))
        m = re.search(r"(-?\d+)\s*°?\s*f", ql)
        if m:
            f = int(m.group(1))
            return int(round((f - 32) * 5.0 / 9.0))
        return None

    def _encode_bucket_purchase_temp(self, kind, threshold_c):
        t = int(threshold_c)
        return (100_000 + t) if kind == "higher" else (200_000 + t)

    def model_prob_bucket_or_higher(self, threshold_c, peak, sigma):
        sigma = max(0.25, float(sigma))
        def cdf(x):
            return 0.5 * (1.0 + math.erf((x - peak) / (sigma * math.sqrt(2.0))))
        p = 1.0 - cdf(float(threshold_c) - 0.5)
        return max(0.0, min(1.0, p))

    def model_prob_bucket_or_below(self, threshold_c, peak, sigma):
        sigma = max(0.25, float(sigma))
        def cdf(x):
            return 0.5 * (1.0 + math.erf((x - peak) / (sigma * math.sqrt(2.0))))
        p = cdf(float(threshold_c) + 0.5)
        return max(0.0, min(1.0, p))

    def gfs_dict_from_real_forecast(self, real_gfs):
        """Минимальный gfs для событий без exact-рынков — только бакеты."""
        if not real_gfs or "peak" not in real_gfs:
            return None
        peak_temp = real_gfs["peak"]
        sigma = float(real_gfs.get("sigma", GFS_STD))
        return {
            "mean": peak_temp,
            "median": peak_temp,
            "peak": peak_temp,
            "ladder": [],
            "core_temps": [],
            "tail_temps": [],
            "df": pd.DataFrame({"temp": [], "prob": []}),
            "sigma": sigma,
            "model_count": real_gfs.get("model_count", 1),
            "confidence": float(real_gfs.get("confidence", 0.5)),
            "risk_tier": real_gfs.get("risk_tier", "unknown"),
            "size_multiplier": float(real_gfs.get("size_multiplier", 0.25)),
            "tails_allowed": bool(real_gfs.get("tails_allowed", False)),
            "hours_to_resolution": real_gfs.get("hours_to_resolution"),
            "spread": float(real_gfs.get("spread", 0.0) or 0.0),
            "source": "gfs",
            "bucket_only": True,
        }

    def reconcile_forecast_learning(self, all_positions):
        now = datetime.now(timezone.utc)
        resolved_exact = {}

        for pos in all_positions:
            ctx = self.parse_position_context(pos)
            end_date = ctx.get('end_date')
            if not ctx.get('city') or ctx.get('temp') is None or not end_date or end_date > now:
                continue
            if not self.is_exact_temperature_market(pos.get('title', '')):
                continue

            try:
                cur_price = float(pos.get('curPrice', 0) or 0)
            except Exception:
                cur_price = 0.0
            outcome = str(pos.get('outcome', '') or '').lower()
            redeemable = bool(pos.get('redeemable', False))
            if outcome == 'yes' and (redeemable or cur_price >= 0.9):
                event_date = end_date.date().isoformat()
                key = f"{ctx['city']}|{event_date}"
                resolved_exact[key] = {
                    'city': ctx['city'],
                    'event_date': event_date,
                    'actual_temp': ctx['temp'],
                }

        updates = 0
        for key, outcome_info in resolved_exact.items():
            if key in self.forecast_learning_index:
                continue

            snapshot = self.forecast_snapshot_index.get(key)
            learning_record = {
                'city': outcome_info['city'],
                'event_date': outcome_info['event_date'],
                'actual_temp': outcome_info['actual_temp'],
                'snapshot_found': bool(snapshot),
                'recorded_at': datetime.now(timezone.utc).isoformat(),
            }

            if snapshot:
                predicted_peak = int(snapshot.get('peak', outcome_info['actual_temp']))
                hours_to_resolution = snapshot.get('hours_to_resolution')
                self.record_gfs_error(
                    outcome_info['city'],
                    predicted_peak,
                    outcome_info['actual_temp'],
                    hours_to_resolution=hours_to_resolution,
                )
                learning_record.update({
                    'predicted_peak': predicted_peak,
                    'hours_to_resolution': hours_to_resolution,
                    'lead_bucket': snapshot.get('lead_bucket'),
                    'confidence': snapshot.get('confidence'),
                    'revision_peak': snapshot.get('revision_peak'),
                    'historical_mae': snapshot.get('historical_mae'),
                })
                self.write_telemetry("forecast_outcome", {
                    'city': outcome_info['city'],
                    'date': outcome_info['event_date'],
                    'actual_temp': outcome_info['actual_temp'],
                    'predicted_peak': predicted_peak,
                    'hours_to_resolution': hours_to_resolution,
                    'lead_bucket': snapshot.get('lead_bucket'),
                    'confidence': snapshot.get('confidence'),
                    'revision_peak': snapshot.get('revision_peak'),
                })

            self.forecast_learning_index[key] = learning_record
            updates += 1

        if updates:
            self.save_forecast_learning_index()
            logger.info(f"   🧠 Forecast learning updates: {updates}")

    def check_city_stoploss(self, city):
        """
        Проверяет заблокирован ли город из-за серии поражений
        Возвращает (blocked: bool, reason: str)
        """
        city_lower = city.lower()
        tracker = self.city_loss_tracker.get(city_lower)
        
        if not tracker:
            return False, "OK"
        
        # Проверяем срок блокировки
        blocked_until = tracker.get('blocked_until', '')
        if blocked_until:
            try:
                unblock_date = datetime.strptime(blocked_until, '%Y-%m-%d')
                if datetime.now() < unblock_date:
                    losses = tracker.get('losses', 0)
                    return True, f"City blocked ({losses} losses, unblock {blocked_until})"
                else:
                    # Блокировка истекла, сбрасываем
                    del self.city_loss_tracker[city_lower]
                    self.save_city_loss_tracker()
                    return False, "OK"
            except:
                return False, "OK"
        
        return False, "OK"

    def record_city_loss(self, city):
        """Записывает поражение города"""
        city_lower = city.lower()
        tracker = self.city_loss_tracker.get(city_lower, {'losses': 0})
        tracker['losses'] = tracker.get('losses', 0) + 1
        tracker['last_loss'] = datetime.now().isoformat()
        
        # Если достигли лимита — блокируем
        if tracker['losses'] >= CITY_STOP_LOSS:
            unblock_date = datetime.now() + timedelta(days=CITY_STOP_LOSS_DAYS)
            tracker['blocked_until'] = unblock_date.strftime('%Y-%m-%d')
            logger.warning(f"   🚫 {city.upper()} заблокирован на {CITY_STOP_LOSS_DAYS} дней ({tracker['losses']} поражений)")
        
        self.city_loss_tracker[city_lower] = tracker
        self.save_city_loss_tracker()

    def record_city_win(self, city):
        """Записывает победу города (сбрасывает счётчик поражений)"""
        city_lower = city.lower()
        if city_lower in self.city_loss_tracker:
            del self.city_loss_tracker[city_lower]
            self.save_city_loss_tracker()

    def fetch_open_meteo_models(self, city, target_date, lat, lon):
        now_utc = datetime.now(timezone.utc)
        if self.open_meteo_backoff_until and now_utc < self.open_meteo_backoff_until:
            remaining = (self.open_meteo_backoff_until - now_utc).total_seconds()
            logger.debug(
                "Open-Meteo cooldown (~%.0fs left), skip ensemble for %s",
                remaining,
                city,
            )
            return {}, {}

        min_iv = OPEN_METEO_MIN_REQUEST_INTERVAL_SEC
        if min_iv > 0 and self.open_meteo_last_request_monotonic > 0:
            elapsed = time.monotonic() - self.open_meteo_last_request_monotonic
            if elapsed < min_iv:
                time.sleep(min_iv - elapsed)

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m",
            "models": "gfs_global,ecmwf_ifs025,ukmo_seamless,bom_access_global",
            "forecast_days": 7,
            "timezone": "auto"
        }

        resp = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, timeout=25 if attempt else 15)
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    logger.warning("   ⚠️ Open-Meteo timeout — повтор запроса…")
                    continue
                logger.warning("   ⚠️ Open-Meteo timeout после повтора")
                return {}, {}
        self.open_meteo_last_request_monotonic = time.monotonic()
        if resp is None:
            return {}, {}
        if resp.status_code != 200:
            if resp.status_code == 429:
                self.open_meteo_backoff_until = datetime.now(timezone.utc) + timedelta(seconds=OPEN_METEO_RATE_LIMIT_BACKOFF_SEC)
                logger.warning(
                    f"   ⚠️ Open-Meteo API rate limit 429 — включаем cooldown на "
                    f"{OPEN_METEO_RATE_LIMIT_BACKOFF_SEC}s"
                )
            else:
                logger.warning(f"   ⚠️ Open-Meteo API error: {resp.status_code}")
            return {}, {}

        data = resp.json()
        daily = data.get('daily', {})
        hourly = data.get('hourly', {})
        if not daily or 'time' not in daily:
            return {}, {}

        dates = daily.get('time', [])
        try:
            date_idx = dates.index(target_date)
        except ValueError:
            logger.warning(f"   ⚠️ Дата {target_date} не найдена в Open-Meteo прогнозе")
            return {}, {}

        model_max_temps = {}
        model_hourly = {}
        model_keys = [
            ('gfs_global', 'temperature_2m_max_gfs_global', 'temperature_2m_gfs_global'),
            ('ecmwf_ifs025', 'temperature_2m_max_ecmwf_ifs025', 'temperature_2m_ecmwf_ifs025'),
            ('ukmo_seamless', 'temperature_2m_max_ukmo_seamless', 'temperature_2m_ukmo_seamless'),
            ('bom_access', 'temperature_2m_max_bom_access_global', 'temperature_2m_bom_access_global'),
        ]

        for model_name, daily_key, hourly_key in model_keys:
            daily_temps = daily.get(daily_key, [])
            if daily_temps and len(daily_temps) > date_idx and daily_temps[date_idx] is not None:
                model_max_temps[model_name] = daily_temps[date_idx]

                hourly_temps = hourly.get(hourly_key, [])
                hourly_times = hourly.get('time', [])
                day_hourly = []
                for j, t_str in enumerate(hourly_times):
                    if t_str.startswith(target_date):
                        day_hourly.append(hourly_temps[j])
                model_hourly[model_name] = day_hourly

        return model_max_temps, model_hourly

    def fetch_metno_models(self, city, target_date, lat, lon):
        url = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
        headers = {"User-Agent": METNO_USER_AGENT}
        params = {"lat": lat, "lon": lon}

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"   ⚠️ met.no API error: {resp.status_code}")
                return {}, {}
            data = resp.json()
        except Exception as e:
            logger.warning(f"   ⚠️ met.no forecast error for {city}: {e}")
            return {}, {}

        timeseries = (((data or {}).get("properties") or {}).get("timeseries") or [])
        hourly_values = []
        rolling_max_values = []

        for entry in timeseries:
            ts = str(entry.get("time", ""))
            if not ts.startswith(target_date):
                continue
            details = (((entry.get("data") or {}).get("instant") or {}).get("details") or {})
            temp = details.get("air_temperature")
            if temp is not None:
                hourly_values.append(float(temp))
            next_6 = (((entry.get("data") or {}).get("next_6_hours") or {}).get("details") or {})
            next_max = next_6.get("air_temperature_max")
            if next_max is not None:
                rolling_max_values.append(float(next_max))

        if not hourly_values and not rolling_max_values:
            logger.warning(f"   ⚠️ Нет дневных данных met.no для {city} {target_date}")
            return {}, {}

        metno_max = max(rolling_max_values or hourly_values)
        return {"metno_locationforecast": metno_max}, {"metno_locationforecast": hourly_values}

    def get_gfs_forecast(self, city, event_date_str, track_history=True):
        """
        🌡️ ENSEMBLE FORECAST через Open-Meteo Multi-Model API
        Модели: GFS (NOAA) + ECMWF (European) + UKMO (British)
        Возвращает: {
            'peak': int,          # Ensemble median max temp (°C)
            'forecast_max': float,# Ensemble mean max temp
            'forecast': list,     # Ensemble mean hourly temps
            'spread': float,      # Std между моделями (uncertainty)
            'sigma': float,       # Dynamic σ для edge модели
            'models': dict,       # {model_name: max_temp}
            'model_count': int,   # Сколько моделей доступно
            'source': 'ensemble'  # Источник
        }
        """
        city_lower = city.lower()
        coord_pack = get_forecast_coordinates(city)
        if not coord_pack:
            logger.warning(f"   ⚠️ Нет координат для {city}")
            return None
        lat, lon, _coord_src = coord_pack

        # Парсим дату события
        try:
            if 'T' in event_date_str:
                event_date = datetime.fromisoformat(event_date_str.split('.')[0].replace('Z', ''))
                if event_date.tzinfo is None:
                    event_date = event_date.replace(tzinfo=timezone.utc)
            else:
                event_date = datetime.strptime(event_date_str[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except:
            logger.warning(f"   ⚠️ Не удалось распарсить дату: {event_date_str}")
            return None

        try:
            target_date = event_date.strftime('%Y-%m-%d')
            proactive_cache = self.get_cached_real_forecast(
                city,
                event_date_str,
                max_age_sec=self.get_forecast_refresh_interval(event_date),
            )
            if proactive_cache:
                logger.info(
                    f"   ♻️ {city}: используем свежий forecast cache "
                    f"(age={proactive_cache.get('cache_age_sec', 0):.0f}s)"
                )
                return proactive_cache

            model_max_temps = {}
            model_hourly = {}

            open_meteo_models, open_meteo_hourly = self.fetch_open_meteo_models(city, target_date, lat, lon)
            metno_models, metno_hourly = self.fetch_metno_models(city, target_date, lat, lon)

            model_max_temps.update(open_meteo_models)
            model_max_temps.update(metno_models)
            model_hourly.update(open_meteo_hourly)
            model_hourly.update(metno_hourly)

            if not model_max_temps:
                cached_forecast = self.get_cached_real_forecast(city, event_date_str)
                if cached_forecast:
                    logger.warning(
                        f"   ♻️ {city}: все forecast APIs недоступны, используем cached real forecast "
                        f"(age={cached_forecast.get('cache_age_sec', 0):.0f}s)"
                    )
                    return cached_forecast
                logger.warning(f"   ⚠️ Нет данных ни от одного forecast source для {city}")
                return None

            model_weights = self.get_city_model_weights(city_lower, list(model_max_temps.keys()))

            # OUTLIER PROTECTION: если одна модель резко выбивается, убираем её до расчёта confidence.
            if len(model_max_temps) >= 3:
                prelim_mean = self.weighted_mean(model_max_temps, model_weights)
                prelim_std = self.weighted_std(model_max_temps, model_weights, mean_value=prelim_mean)
                outlier_threshold = max(1.0, prelim_std * 1.75)
                clean_model_max_temps = {
                    name: value
                    for name, value in model_max_temps.items()
                    if abs(float(value) - prelim_mean) <= outlier_threshold
                }
                if len(clean_model_max_temps) >= 2 and len(clean_model_max_temps) < len(model_max_temps):
                    removed_models = sorted(set(model_max_temps.keys()) - set(clean_model_max_temps.keys()))
                    logger.info(f"   🧹 Outlier trim {city}: убрали {', '.join(removed_models)}")
                    model_max_temps = clean_model_max_temps
                    model_hourly = {name: values for name, values in model_hourly.items() if name in model_max_temps}
                    model_weights = self.get_city_model_weights(city_lower, list(model_max_temps.keys()))

            # 📊 ENSEMBLE CALCULATION
            ensemble_mean = self.weighted_mean(model_max_temps, model_weights)
            ensemble_median = ensemble_mean
            ensemble_std = self.weighted_std(model_max_temps, model_weights, mean_value=ensemble_mean)

            # Ensemble forecast (взвешенное почасовое среднее по всем доступным моделям)
            all_hourly = list(model_hourly.values())
            if all_hourly:
                n_hours = max(len(h) for h in all_hourly)
                ensemble_hourly = []
                for h in range(n_hours):
                    weighted_vals = []
                    for model_name, model_values in model_hourly.items():
                        if h < len(model_values) and model_values[h] is not None:
                            weighted_vals.append((float(model_values[h]), model_weights.get(model_name, 0.0)))
                    total_weight = sum(weight for _, weight in weighted_vals)
                    if weighted_vals and total_weight > 0:
                        ensemble_hourly.append(sum(value * weight for value, weight in weighted_vals) / total_weight)
                    else:
                        ensemble_hourly.append(None)
            else:
                ensemble_hourly = []

            # 🎯 DYNAMIC SIGMA
            # Зависит от: (1) spread между моделями, (2) время до разрешения
            hours_to_resolution = (event_date - datetime.now(timezone.utc)).total_seconds() / 3600

            # Базовая σ зависит от горизонта прогноза
            if hours_to_resolution <= 6:
                base_sigma = 0.8
            elif hours_to_resolution <= 24:
                base_sigma = 1.5
            elif hours_to_resolution <= 48:
                base_sigma = 2.0
            elif hours_to_resolution <= 72:
                base_sigma = 2.5
            else:
                base_sigma = 3.5

            # Если модели РАСХОДЯТСЯ (>1.5°C std) → расширяем σ
            if ensemble_std > 1.5:
                dynamic_sigma = base_sigma * 1.3  # +30% за разногласие
            else:
                dynamic_sigma = base_sigma

            weighted_peak = round(ensemble_mean)
            snapshot_key = f"{city_lower}|{target_date}"
            previous_snapshot = self.forecast_snapshot_index.get(snapshot_key)
            revision_peak = None
            revision_mean = None
            if previous_snapshot:
                try:
                    revision_peak = abs(float(weighted_peak) - float(previous_snapshot.get('peak', weighted_peak)))
                    revision_mean = abs(float(ensemble_mean) - float(previous_snapshot.get('forecast_max', ensemble_mean)))
                except Exception:
                    revision_peak = None
                    revision_mean = None

            hist_stats, lead_bucket, bias_scope = self.get_bias_stats(city_lower, hours_to_resolution)
            confidence = self.calculate_confidence_score(
                city_lower,
                ensemble_std,
                hours_to_resolution,
                revision_peak=revision_peak,
                hist_stats=hist_stats,
            )
            confidence_profile = self.get_confidence_profile(city_lower, confidence)
            provider_sources = sorted(
                {
                    "metno" if name == "metno_locationforecast" else "open_meteo"
                    for name in model_max_temps.keys()
                }
            )

            model_names_str = ', '.join(model_max_temps.keys())
            model_weights_str = ', '.join(f"{name}:{model_weights.get(name, 0.0):.2f}" for name in model_max_temps.keys())
            logger.info(f"   🌍 Ensemble ({len(model_max_temps)} models from {', '.join(provider_sources)}: {model_names_str})")
            logger.info(f"      Max temps: {model_max_temps}")
            logger.info(f"      Weights: {model_weights_str}")
            logger.info(f"      Weighted mean: {ensemble_mean:.1f}°C, peak: {weighted_peak}°C")
            logger.info(
                f"      Spread (std): {ensemble_std:.2f}°C, Dynamic σ: {dynamic_sigma:.2f}°C, "
                f"confidence: {confidence:.2f}, tier: {confidence_profile['risk_tier']}, "
                f"revision_peak: {revision_peak if revision_peak is not None else 'n/a'}, hist_scope: {bias_scope}"
            )
            forecast_result = {
                'peak': weighted_peak,
                'forecast_max': ensemble_mean,
                'forecast_median': ensemble_median,
                'forecast': ensemble_hourly,
                'spread': ensemble_std,
                'sigma': dynamic_sigma,
                'models': model_max_temps,
                'model_weights': model_weights,
                'model_count': len(model_max_temps),
                'confidence': confidence,
                'risk_tier': confidence_profile['risk_tier'],
                'size_multiplier': confidence_profile['size_multiplier'],
                'tails_allowed': confidence_profile['tails_allowed'],
                'should_skip': confidence_profile['should_skip'],
                'hours_to_resolution': hours_to_resolution,
                'lead_bucket': lead_bucket,
                'historical_bias': float(hist_stats.get('bias', 0.0)) if hist_stats else 0.0,
                'historical_mae': float(hist_stats.get('mae', 0.0)) if hist_stats else None,
                'historical_count': int(hist_stats.get('count', 0)) if hist_stats else 0,
                'revision_peak': revision_peak,
                'revision_mean': revision_mean,
                'source': 'ensemble',
                'providers': provider_sources,
                'date': target_date,
                'raw_peak': weighted_peak,
                'bias_corrected': False
            }
            if track_history:
                snapshot = self.save_forecast_snapshot(city_lower, event_date_str, forecast_result)
                forecast_result['revision_peak'] = snapshot.get('revision_peak')
                forecast_result['revision_mean'] = snapshot.get('revision_mean')
                self.write_telemetry("forecast_snapshot", {
                    'city': city_lower,
                    'date': target_date,
                    'peak': weighted_peak,
                    'forecast_max': ensemble_mean,
                    'spread': ensemble_std,
                    'sigma': dynamic_sigma,
                    'confidence': confidence,
                    'risk_tier': confidence_profile['risk_tier'],
                    'lead_bucket': lead_bucket,
                    'historical_mae': forecast_result.get('historical_mae'),
                    'historical_count': forecast_result.get('historical_count'),
                    'revision_peak': snapshot.get('revision_peak'),
                    'revision_mean': snapshot.get('revision_mean'),
                    'providers': provider_sources,
                })
            self.store_cached_real_forecast(city_lower, event_date_str, forecast_result)
            return forecast_result

        except Exception as e:
            logger.warning(f"   ⚠️ Open-Meteo Ensemble ошибка для {city}: {e}")
            return None

    def load_gfs_history(self):
        """Загружает историю GFS пиков из файла"""
        if os.path.exists(self.gfs_history_file):
            try:
                with open(self.gfs_history_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_gfs_history(self, history):
        """Сохраняет GFS пики в файл"""
        # Конвертируем numpy int64 в обычный int
        clean_history = {}
        for k, v in history.items():
            clean_history[k] = int(v)
        with open(self.gfs_history_file, 'w') as f:
            json.dump(clean_history, f, indent=2)

    def check_gfs_stability(self, city, event_date_str, current_peak):
        """
        🌪️ GFS STABILITY CHECK:
        Сравнивает текущий пик GFS с предыдущим запуском.
        Возвращает (stable: bool, drift: float)
        """
        history = self.load_gfs_history()
        
        # Ключ для хранения: город + дата
        key = f"{city}_{event_date_str[:10]}"  # YYYY-MM-DD
        
        if key in history:
            prev_peak = history[key]
            drift = abs(current_peak - prev_peak)
            
            if drift > GFS_MAX_DRIFT:
                return False, drift
            return True, drift
        
        # Первый запуск — сохраняем и считаем стабильным
        history[key] = current_peak
        self.save_gfs_history(history)
        return True, 0.0

    def update_gfs_history(self, city, event_date_str, current_peak):
        """Обновляет историю GFS после успешного анализа"""
        history = self.load_gfs_history()
        key = f"{city}_{event_date_str[:10]}"
        history[key] = current_peak
        self.save_gfs_history(history)
        
    def get_usdc_balance_rpc(self, wallet):
        """
        Получает реальный баланс USDC.e через Polygon RPC (с fallback на 3 сервера)
        """
        try:
            if not wallet:
                return None

            # USDC.e Contract on Polygon (Polymarket collateral)
            usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            # balanceOf(address) signature
            func_hash = "0x70a08231"
            
            # Правильное форматирование адреса (64 символа)
            addr_hex = wallet.lower().replace("0x", "")
            if len(addr_hex) != 40: return None
            padded_addr = addr_hex.zfill(64)
            data = func_hash + padded_addr
            
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": usdc_contract, "data": data}, "latest"],
                "id": 1
            }
            
            # Список RPC (если один упал, пробуем другой)
            rpc_urls = [
                "https://polygon.publicnode.com",
                "https://polygon-rpc.com",
                "https://polygon.llamarpc.com",
                "https://rpc.ankr.com/polygon"
            ]
            
            for rpc_url in rpc_urls:
                try:
                    r = requests.post(
                        rpc_url,
                        json=payload,
                        timeout=8,
                        headers={"Content-Type": "application/json", "Accept": "application/json"}
                    )
                    r.raise_for_status()
                    res = r.json()
                    
                    if "result" in res and res["result"] and res["result"] != "0x":
                        bal_int = int(res["result"], 16)
                        # USDC has 6 decimals
                        return bal_int / 1e6
                except Exception as e:
                    logger.debug(f"   RPC balance miss {rpc_url}: {e}")
                    continue  # Пробуем следующий RPC
                    
        except Exception as e:
            logger.warning(f"⚠️ Ошибка проверки баланса: {e}")
        return None

    def get_balance_and_positions(self):
        """Получает баланс и ВСЕ позиции"""
        url = f"{self.data_url}/positions"
        params = {"user": FUNDER_ADDRESS, "sizeThreshold": 0.001}
        timeout = (15, 90)
        positions = None
        for attempt in range(1, 5):
            try:
                response = requests.get(url, params=params, verify=False, timeout=timeout)
                response.raise_for_status()
                positions = response.json()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(f"   ⚠️ positions API (попытка {attempt}/4): {e}")
                if attempt == 4:
                    logger.error(f"Ошибка получения данных: {e}")
                    return None, None
                time.sleep(min(2 * attempt, 12))
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code in (408, 429, 500, 502, 503, 504) and attempt < 4:
                    logger.warning(f"   ⚠️ positions API HTTP {code} (попытка {attempt}/4)")
                    time.sleep(min(2 * attempt, 12))
                else:
                    logger.error(f"Ошибка получения данных: {e}")
                    return None, None
            except Exception as e:
                logger.error(f"Ошибка получения данных: {e}")
                return None, None

        if positions is None:
            return None, None

        try:
            real_balance = get_wallet_balance_api(FUNDER_ADDRESS)
            if real_balance is None:
                real_balance = self.get_usdc_balance_rpc(FUNDER_ADDRESS)

            if real_balance is not None:
                current_bankroll = real_balance
                self.session_budget = current_bankroll * SESSION_BUDGET_PCT
                logger.info(f"   💰 Реальный баланс кошелька: ${current_bankroll:.2f}")
            else:
                current_bankroll = BANKROLL
                self.session_budget = current_bankroll * SESSION_BUDGET_PCT
                logger.warning(f"   ⚠️ Не удалось получить баланс, используем конфиг: ${current_bankroll}")

            return current_bankroll, positions
        except Exception as e:
            logger.error(f"Ошибка получения данных: {e}")
            return None, None

    def get_active_positions(self, all_positions):
        """
        👻 GHOST LIMIT FIX:
        Фильтрует позиции. Возвращает только те, что ЕЩЕ не закрылись.
        """
        now = datetime.now(timezone.utc)
        active = []
        
        for pos in all_positions:
            end_str = pos.get('endDate', '')
            if end_str:
                try:
                    end_str_clean = end_str.split('.')[0].replace('Z', '')
                    # Если дата в формате Y-m-d, добавляем ей UTC
                    if 'T' in end_str_clean:
                        end_date = datetime.fromisoformat(end_str_clean)
                    else:
                        end_date = datetime.strptime(end_str_clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    
                    # Если дата в будущем — позиция активна
                    if end_date > now:
                        active.append(pos)
                except Exception as e:
                    # ВАЖНО: Если не можем распарсить дату, считаем позицию АКТИВНОЙ (безопаснее)
                    logger.warning(f"⚠️ Ошибка парсинга даты позиции ({end_str}): {e}. Считаем активной.")
                    active.append(pos)
        return active

    def maybe_auto_redeem_resolved(self, all_positions):
        """
        Автовывод выигрышных/resolved позиций on-chain (redeem USDC.e на Polygon).
        Работает только при LIVE (не DRY_RUN) и ENABLE_AUTO_REDEEM.
        За один проход — не больше AUTO_REDEEM_MAX_PER_SCAN транзакций (газ + RPC).
        Между попытками — не чаще чем раз в AUTO_REDEEM_MIN_INTERVAL_SEC (если > 0).
        """
        if not ENABLE_AUTO_REDEEM or AUTO_REDEEM_MAX_PER_SCAN <= 0:
            return
        if DRY_RUN:
            return
        if not PRIVATE_KEY:
            logger.debug("AUTO_REDEEM: пропуск (нет POLYMARKET_PK)")
            return
        import time

        if AUTO_REDEEM_MIN_INTERVAL_SEC > 0:
            now_m = time.monotonic()
            if self._last_auto_redeem_ts is not None:
                elapsed = now_m - self._last_auto_redeem_ts
                if elapsed < AUTO_REDEEM_MIN_INTERVAL_SEC:
                    logger.debug(
                        "AUTO_REDEEM: cooldown %.0fs / %ds — пропуск",
                        elapsed,
                        AUTO_REDEEM_MIN_INTERVAL_SEC,
                    )
                    return
        try:
            from poly_ctf_redeem import run_auto_redeem_batch
        except ImportError as e:
            logger.warning(f"AUTO_REDEEM: не удалось импортировать poly_ctf_redeem: {e}")
            return
        rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
        try:
            results = run_auto_redeem_batch(
                all_positions,
                wallet=FUNDER_ADDRESS,
                private_key=PRIVATE_KEY,
                rpc_url=rpc,
                max_redeems=AUTO_REDEEM_MAX_PER_SCAN,
                only_weather=AUTO_REDEEM_WEATHER_ONLY,
                force_ctf=False,
            )
        except Exception as e:
            logger.warning(f"AUTO_REDEEM: ошибка пакета: {e}")
            return
        # Cooldown только после реальной попытки redeem (есть кандидаты); иначе следующий скан снова проверит список
        if AUTO_REDEEM_MIN_INTERVAL_SEC > 0 and results:
            self._last_auto_redeem_ts = time.monotonic()
        if not results:
            return
        print(f"\n{C.BOLD}💰 Auto-redeem (on-chain):{C.ENDC}")
        for r in results:
            err = r.get("error")
            txh = r.get("tx_hash")
            title = (r.get("title") or "")[:70]
            if err:
                logger.warning(f"   ⚠️ AUTO_REDEEM: {title} — {err}")
            elif txh:
                logger.info(f"   ✅ AUTO_REDEEM {txh} — {title}")
                print(f"   {C.GREEN}✓{C.ENDC} {txh[:18]}…  {title}")
            elif r.get("skipped"):
                logger.debug(f"   AUTO_REDEEM skip (0 on-chain balance): {r.get('token_id', '')[:24]}…")

    def maybe_auto_dump_dead_positions(self, all_positions):
        """
        Полный SELL позиций, которые по снимку API уже не имеет смысла держать:
        endDate в прошлом, redeemable=false, 0 < curPrice ≤ AUTO_DUMP_DEAD_MAX_CUR.
        При curPrice ≤ 0 позиция не кандидат — не дергаем стакан и не шлём SELL (бессмысленно и лишние подписи/RPC).
        Как «discard» в positions_losers_report.py (on-chain redeem тут не помогает — только ликвидность bid).

        LIVE: нужен ClobClient; DRY_RUN — только симуляция place_sell_order.
        Ограничение по частоте: AUTO_DUMP_DEAD_MIN_INTERVAL_SEC, за цикл — не больше AUTO_DUMP_DEAD_MAX_PER_SCAN.
        Если bid_px×contracts < AUTO_DUMP_DEAD_MIN_NOTIONAL_USD — не шлём SELL (избегать микро-выручки).
        """
        if not ENABLE_AUTO_DUMP_DEAD or AUTO_DUMP_DEAD_MAX_PER_SCAN <= 0:
            return
        if not all_positions:
            return
        if AUTO_DUMP_DEAD_MIN_INTERVAL_SEC > 0:
            now_m = time.monotonic()
            if self._last_auto_dump_dead_ts is not None:
                elapsed = now_m - self._last_auto_dump_dead_ts
                if elapsed < AUTO_DUMP_DEAD_MIN_INTERVAL_SEC:
                    logger.debug(
                        "AUTO_DUMP_DEAD: cooldown %.0fs / %ds — пропуск",
                        elapsed,
                        AUTO_DUMP_DEAD_MIN_INTERVAL_SEC,
                    )
                    return

        now = datetime.now(timezone.utc)
        candidates: list[dict] = []
        for pos in all_positions:
            if AUTO_DUMP_DEAD_WEATHER_ONLY and not is_weather_market_title(pos.get("title")):
                continue
            if bool(pos.get("redeemable", False)):
                continue
            end_dt = _position_end_datetime_utc(pos)
            if end_dt is None or end_dt > now:
                continue
            try:
                cur = float(pos.get("curPrice") or 0)
            except (TypeError, ValueError):
                cur = 0.0
            # Нулевая цена = нет смысла и нет исполнения — не тратим запросы к стакану / подписи
            if cur <= 0:
                continue
            if cur > AUTO_DUMP_DEAD_MAX_CUR + 1e-12:
                continue
            try:
                sz = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if sz < 1e-9:
                continue
            tid = pos.get("asset") or pos.get("asset_id") or pos.get("token_id")
            if not tid:
                continue
            candidates.append(pos)

        if not candidates:
            return

        candidates.sort(key=_position_api_mark_usd, reverse=True)

        if not DRY_RUN and not self.client:
            logger.warning("AUTO_DUMP_DEAD: ClobClient не инициализирован — SELL невозможен")
            print(
                f"   {C.WARNING}⚠ AUTO_DUMP_DEAD: нет ClobClient (POLYMARKET_PK / инициализация){C.ENDC}"
            )
            return

        if AUTO_DUMP_DEAD_MIN_INTERVAL_SEC > 0:
            self._last_auto_dump_dead_ts = time.monotonic()

        print(f"\n{C.BOLD}🗑️ Auto-dump dead positions (CLOB SELL):{C.ENDC}")
        done = 0
        for pos in candidates:
            if done >= AUTO_DUMP_DEAD_MAX_PER_SCAN:
                break
            ctx = self.parse_position_context(pos)
            token_id = ctx.get("token_id")
            if not token_id:
                continue
            try:
                sz = float(ctx.get("size") or 0)
            except Exception:
                sz = 0.0
            whole = int(math.floor(sz + 1e-9))
            if whole < 1:
                continue
            fallback = float(pos.get("curPrice") or 0)
            if fallback <= 0:
                continue
            oname = str(ctx.get("outcome_name") or "Yes").strip() or "Yes"
            quote = self.get_cached_quote(
                token_id,
                contracts=max(1, whole),
                fallback_price=fallback,
                market=None,
                outcome_name=oname,
            )
            best_bid = float(quote.get("sell_price") or 0)
            # Нет ликвидности по bid — продавать нечего (как при нулевой цене)
            if best_bid <= 0:
                logger.info(
                    "   AUTO_DUMP_DEAD: нет bid — %s",
                    (ctx.get("title") or "")[:70],
                )
                continue
            bid_px = _clob_price_quantize_4(best_bid)
            if bid_px <= 0:
                logger.info(
                    "   AUTO_DUMP_DEAD: bid после квантования 4 знака = 0 (слишком мелкий bid) — %s",
                    (ctx.get("title") or "")[:70],
                )
                continue
            if AUTO_DUMP_DEAD_MIN_BID > 0 and best_bid + 1e-15 < AUTO_DUMP_DEAD_MIN_BID:
                logger.info(
                    "   AUTO_DUMP_DEAD: bid %.6g < min %.6g — %s",
                    best_bid,
                    AUTO_DUMP_DEAD_MIN_BID,
                    (ctx.get("title") or "")[:60],
                )
                continue
            proceeds_est = bid_px * whole
            if (
                AUTO_DUMP_DEAD_MIN_NOTIONAL_USD > 0
                and proceeds_est + 1e-12 < AUTO_DUMP_DEAD_MIN_NOTIONAL_USD
            ):
                logger.info(
                    "   AUTO_DUMP_DEAD: пропуск (выручка ~$%.4f < min $%.2f — не рентабельно vs газ/пыль): %s",
                    proceeds_est,
                    AUTO_DUMP_DEAD_MIN_NOTIONAL_USD,
                    (ctx.get("title") or "")[:60],
                )
                continue

            if ctx.get("city") and ctx.get("temp") is not None:
                lab = f"{ctx['city'].upper()} {format_purchase_temp_label(ctx['temp'])}"
            else:
                lab = (ctx.get("title") or "")[:56]

            result = place_sell_order(token_id, bid_px, whole, self.client)
            if result.get("status") == "ERROR":
                err = result.get("reason", "?")
                logger.warning(f"   ⚠️ AUTO_DUMP_DEAD: {lab} — {err}")
                print(f"   {C.WARNING}⚠{C.ENDC} {lab}  SELL fail: {err[:80]}")
                continue

            done += 1
            st = result.get("status")
            pr = float(result.get("proceeds", round(bid_px * whole, 2)))
            logger.info(
                "   AUTO_DUMP_DEAD OK: %s | %d контр. @ %.2f¢ → ~$%.2f [%s]",
                lab,
                whole,
                bid_px * 100,
                pr,
                st,
            )
            print(
                f"   {C.GREEN}✓{C.ENDC} dump_dead  {lab}  |  {whole} @ {bid_px * 100:.1f}¢  "
                f"(~${pr:.2f})  [{st}]"
            )
            self.write_telemetry(
                "dump_dead",
                {
                    "label": lab,
                    "token_id": str(token_id),
                    "contracts": whole,
                    "best_bid_raw": best_bid,
                    "bid_px": bid_px,
                    "proceeds_est": pr,
                    "status": st,
                    "title": (ctx.get("title") or "")[:120],
                },
            )
            if TG_ENABLED:
                send_telegram(
                    f"<b>🗑️ AUTO_DUMP_DEAD</b> ({st})\n"
                    f"{lab}\n"
                    f"📦 {whole} @ {bid_px * 100:.1f}¢  ≈ ${pr:.2f}\n"
                    f"<i>{(ctx.get('title') or '')[:100]}</i>"
                )

    def parse_position_context(self, pos):
        title = (pos.get('title') or '').lower()
        city = resolve_trading_city_from_title(title)

        temp = None
        if "°f" in title or "fahrenheit" in title:
            temp = None
        else:
            match = re.search(r"(-?\d+)\s*°\s*c\b", title, re.IGNORECASE)
            if match:
                temp = int(match.group(1))

        end_date = None
        end_str = pos.get('endDate', '')
        if end_str:
            try:
                clean = end_str.split('.')[0].replace('Z', '')
                if 'T' in clean:
                    end_date = datetime.fromisoformat(clean)
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                else:
                    end_date = datetime.strptime(clean, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                end_date = None

        token_id = pos.get('asset') or pos.get('asset_id') or pos.get('token_id')
        try:
            size = float(pos.get('size', 0) or 0)
        except Exception:
            size = 0.0

        try:
            avg_price = float(pos.get('avgPrice', 0) or 0)
        except Exception:
            avg_price = 0.0

        outcome = str(pos.get('outcome', 'Yes') or 'Yes').strip()

        return {
            'title': pos.get('title', ''),
            'city': city,
            'temp': temp,
            'end_date': end_date,
            'token_id': str(token_id) if token_id else None,
            'size': size,
            'avg_price': avg_price,
            'outcome_name': outcome,
        }

    def get_empirical_skill(self, model_count):
        if model_count >= 3:
            return 0.85
        if model_count == 2:
            return 0.80
        return 0.70

    def get_city_risk_tier(self, city):
        city_lower = (city or "").lower()
        if city_lower in LOW_RISK_CITIES:
            return "low"
        if city_lower in HIGH_RISK_CITIES:
            return "high"
        if city_lower in NO_WU_DATA_CITIES:
            return "unknown"
        return "medium"

    def get_city_model_weights(self, city, available_models):
        city_lower = (city or "").lower()
        if city_lower in BOM_FAVORED_CITIES:
            base_weights = BOM_MODEL_WEIGHTS
        elif city_lower in ECMWF_FAVORED_CITIES:
            base_weights = ECMWF_MODEL_WEIGHTS
        elif city_lower in GFS_FAVORED_CITIES:
            base_weights = GFS_MODEL_WEIGHTS
        else:
            base_weights = DEFAULT_MODEL_WEIGHTS

        weights = {model: float(base_weights.get(model, 0.0)) for model in available_models}
        total = sum(weights.values())
        if total <= 0:
            equal_weight = 1.0 / max(1, len(available_models))
            return {model: equal_weight for model in available_models}
        return {model: value / total for model, value in weights.items() if value > 0}

    def weighted_mean(self, value_map, weight_map):
        if not value_map:
            return 0.0
        total_weight = sum(weight_map.get(key, 0.0) for key in value_map.keys())
        if total_weight <= 0:
            return sum(float(v) for v in value_map.values()) / max(1, len(value_map))
        return sum(float(value_map[key]) * weight_map.get(key, 0.0) for key in value_map.keys()) / total_weight

    def weighted_std(self, value_map, weight_map, mean_value=None):
        if not value_map:
            return 0.0
        if mean_value is None:
            mean_value = self.weighted_mean(value_map, weight_map)
        total_weight = sum(weight_map.get(key, 0.0) for key in value_map.keys())
        if total_weight <= 0:
            variance = sum((float(v) - mean_value) ** 2 for v in value_map.values()) / max(1, len(value_map))
        else:
            variance = sum(
                weight_map.get(key, 0.0) * (float(value_map[key]) - mean_value) ** 2
                for key in value_map.keys()
            ) / total_weight
        return math.sqrt(max(0.0, variance))

    def calculate_confidence_score(self, city, spread, hours_to_resolution, revision_peak=None, hist_stats=None):
        confidence = 1.0
        if spread >= 3.0:
            confidence -= 0.35
        elif spread >= 2.0:
            confidence -= 0.20
        elif spread >= 1.0:
            confidence -= 0.10

        risk_tier = self.get_city_risk_tier(city)
        if risk_tier == "low":
            confidence += 0.10
        elif risk_tier == "high":
            confidence -= 0.25
        elif risk_tier == "unknown":
            confidence -= 0.20

        if hours_to_resolution <= 24:
            confidence += 0.05
        elif hours_to_resolution > 72:
            confidence -= 0.10

        if revision_peak is not None:
            if revision_peak >= 3.0:
                confidence -= 0.20
            elif revision_peak >= 2.0:
                confidence -= 0.12
            elif revision_peak >= 1.0:
                confidence -= 0.06

        if hist_stats and hist_stats.get('count', 0) >= MIN_BIAS_BUCKET_COUNT:
            hist_mae = float(hist_stats.get('mae', 0.0))
            hist_bias = abs(float(hist_stats.get('bias', 0.0)))
            if hist_mae >= 4.0:
                confidence -= 0.25
            elif hist_mae >= 3.0:
                confidence -= 0.15
            elif hist_mae >= 2.0:
                confidence -= 0.08
            elif hist_mae <= 1.5:
                confidence += 0.05

            if hist_bias >= 2.0:
                confidence -= 0.05

        return max(0.0, min(1.0, confidence))

    def estimate_bin_probability(self, temp, peak, sigma, model_count, confidence, hours_to_resolution=None, hist_stats=None):
        sigma = max(0.75, float(sigma or GFS_STD))
        raw_prob = self.calc_bucket_probability(temp, peak, sigma)
        empirical_skill = self.get_empirical_skill(model_count)

        if hist_stats and hist_stats.get('count', 0) >= MIN_BIAS_BUCKET_COUNT:
            hist_mae = float(hist_stats.get('mae', 0.0))
            history_multiplier = max(0.50, min(1.00, 1.08 - 0.12 * max(0.0, hist_mae - 1.0)))
        else:
            history_multiplier = 0.95

        confidence_multiplier = 0.55 + (0.45 * max(0.0, min(1.0, confidence)))
        if hours_to_resolution is not None and hours_to_resolution > 72:
            confidence_multiplier *= 0.95

        return raw_prob * empirical_skill * history_multiplier * confidence_multiplier

    def get_confidence_profile(self, city, confidence):
        risk_tier = self.get_city_risk_tier(city)
        risk_multiplier_map = {
            "low": 1.00,
            "medium": 0.70,
            "high": 0.35,
            "unknown": 0.25,
        }
        risk_multiplier = risk_multiplier_map.get(risk_tier, 0.70)

        if confidence >= CONFIDENCE_TAILS_THRESHOLD:
            confidence_multiplier = 1.00
            tails_allowed = True
        elif confidence >= CONFIDENCE_REDUCED_SIZE_THRESHOLD:
            confidence_multiplier = 0.70
            tails_allowed = False
        elif confidence >= CONFIDENCE_SKIP_THRESHOLD:
            confidence_multiplier = 0.40
            tails_allowed = False
        else:
            confidence_multiplier = 0.0
            tails_allowed = False

        if risk_tier in {"high", "unknown"}:
            tails_allowed = False

        return {
            "risk_tier": risk_tier,
            "risk_multiplier": risk_multiplier,
            "confidence_multiplier": confidence_multiplier,
            "size_multiplier": risk_multiplier * confidence_multiplier,
            "tails_allowed": tails_allowed,
            "should_skip": confidence < CONFIDENCE_SKIP_THRESHOLD,
        }

    def get_entry_price_limit(self, risk_tier, confidence, is_tail=False):
        risk_key = str(risk_tier or "medium").lower()
        confidence = max(0.0, min(1.0, float(confidence or 0.0)))

        if is_tail:
            base_limit = TAIL_MAX_PRICE
            tier_limits = {
                "low": TAIL_PRICE_LIMIT_LOW_RISK,
                "medium": TAIL_PRICE_LIMIT_MEDIUM_RISK,
                "high": TAIL_PRICE_LIMIT_HIGH_RISK,
                "unknown": TAIL_PRICE_LIMIT_UNKNOWN_RISK,
            }
            tier_limit = tier_limits.get(risk_key, TAIL_PRICE_LIMIT_MEDIUM_RISK)

            if confidence >= 0.95:
                return round(tier_limit, 4)
            if confidence >= 0.85:
                return round(max(base_limit, tier_limit - 0.01), 4)
            return round(base_limit, 4)

        base_limit = MAX_CONTRACT_PRICE
        tier_limits = {
            "low": CORE_PRICE_LIMIT_LOW_RISK,
            "medium": CORE_PRICE_LIMIT_MEDIUM_RISK,
            "high": CORE_PRICE_LIMIT_HIGH_RISK,
            "unknown": CORE_PRICE_LIMIT_UNKNOWN_RISK,
        }
        tier_limit = tier_limits.get(risk_key, CORE_PRICE_LIMIT_MEDIUM_RISK)

        if confidence >= 0.95:
            limit = tier_limit
        elif confidence >= 0.85:
            limit = max(base_limit, tier_limit - 0.02)
        elif confidence >= 0.70:
            limit = max(base_limit, tier_limit - 0.04)
        else:
            limit = base_limit

        return round(limit, 4)

    def get_no_price_limit(self, risk_tier, confidence):
        risk_key = str(risk_tier or "medium").lower()
        confidence = max(0.0, min(1.0, float(confidence or 0.0)))
        tier_limits = {
            "low": NO_MAX_PRICE_LOW_RISK,
            "medium": NO_MAX_PRICE_MEDIUM_RISK,
            "high": NO_MAX_PRICE_HIGH_RISK,
            "unknown": NO_MAX_PRICE_UNKNOWN_RISK,
        }
        limit = float(tier_limits.get(risk_key, NO_MAX_PRICE_MEDIUM_RISK))
        if limit <= 0:
            return 0.0
        if confidence >= 0.95:
            return round(limit, 4)
        if confidence >= 0.90:
            return round(max(0.60, limit - 0.04), 4)
        return round(max(0.55, limit - 0.08), 4)

    def compute_signal_priority(self, signal):
        price = float(signal.get('price', 0.0) or 0.0)
        edge = float(signal.get('edge', 0.0) or 0.0)
        confidence = float(signal.get('confidence', 0.0) or 0.0)
        model_prob = float(signal.get('model_prob', 0.0) or 0.0)
        risk_tier = str(signal.get('risk_tier') or 'unknown').lower()
        reprice_count = int(signal.get('reprice_count', 0) or 0)

        score = edge * 100.0
        score += confidence * 20.0
        score += min(model_prob, 0.40) * 12.0
        score += min(reprice_count, MAX_PENDING_REPRICES) * 3.0

        if price <= 0.12:
            score += 6.0
        elif price <= 0.20:
            score += 2.0
        else:
            score -= min(10.0, (price - 0.20) * 25.0)

        if signal.get('is_tail', False):
            score -= 7.0
        if signal.get('is_no_hedge', False):
            score -= 3.0

        score += {
            'low': 4.0,
            'medium': 1.5,
            'high': -4.0,
            'unknown': -1.0,
        }.get(risk_tier, 0.0)
        return round(score, 4)

    def prioritize_signals(self, signals):
        ranked = []
        for signal in signals:
            signal['priority_score'] = self.compute_signal_priority(signal)
            ranked.append(signal)

        ranked.sort(
            key=lambda s: (
                float(s.get('priority_score', 0.0)),
                float(s.get('edge', 0.0)),
                float(s.get('confidence', 0.0)),
                -float(s.get('price', 1.0)),
            ),
            reverse=True,
        )

        aggressive_left = max(0, int(AGGRESSIVE_MAX_SIGNALS_PER_SCAN))
        for signal in ranked:
            reprice_count = int(signal.get('reprice_count', 0) or 0)
            qualifies = (
                aggressive_left > 0
                and not signal.get('is_tail', False)
                and float(signal.get('confidence', 0.0) or 0.0) >= AGGRESSIVE_MIN_CONFIDENCE
                and (
                    float(signal.get('edge', 0.0) or 0.0) >= AGGRESSIVE_EDGE_THRESHOLD
                    or reprice_count > 0
                )
                and str(signal.get('risk_tier') or 'unknown').lower() in {'low', 'medium'}
            )
            signal['aggressive'] = bool(qualifies)
            if qualifies:
                aggressive_left -= 1
        return ranked

    def _count_purchased_strikes_city_date(self, city: str, date_key: str) -> int:
        c = (city or "").lower()
        d = (date_key or "")[:10]
        if not c or len(d) < 10:
            return 0
        return sum(
            1
            for (ct, temp, dt) in self.purchased_cache
            if ct == c
            and str(dt)[:10] == d
            and not (AI_WEATHER and is_bucket_encoded_temp(temp))
        )

    def _exact_temps_in_cache(self, city: str, date_key: str) -> set[int]:
        """Distinct exact °C keys в cache (encoded bucket ≥100k не считаются)."""
        c = (city or "").lower()
        d = (date_key or "")[:10]
        out: set[int] = set()
        if not c or len(d) < 10:
            return out
        for (ct, temp, dt) in self.purchased_cache:
            if ct != c or str(dt)[:10] != d:
                continue
            if is_bucket_encoded_temp(temp):
                continue
            try:
                out.add(int(temp))
            except (TypeError, ValueError):
                continue
        return out

    def cap_signals_by_city_date(self, signals):
        """Оставляем не больше MAX_STRIKES_PER_CITY_DATE новых сигналов на пару город+дата за скан."""
        if MAX_STRIKES_PER_CITY_DATE <= 0:
            return signals
        batch = {}
        out = []
        for s in signals:
            city = (s.get("city") or "").lower()
            dk = (s.get("date") or "")[:10]
            if not city or len(dk) < 10:
                out.append(s)
                continue
            ex = self._count_purchased_strikes_city_date(city, dk)
            key = (city, dk)
            used = batch.get(key, 0)
            if ex + used >= MAX_STRIKES_PER_CITY_DATE:
                _hint = " (exact в cache; bucket-ключи ≥100k не считаются)" if AI_WEATHER else ""
                logger.info(
                    f"   ⏭️ Лимит страйков на город/день ({MAX_STRIKES_PER_CITY_DATE}): "
                    f"{city} {dk} — пропуск сигнала (в cache уже {ex}{_hint})"
                )
                continue
            batch[key] = used + 1
            out.append(s)
        return out

    def get_execution_price(self, signal, live_quote, max_price_allowed, min_edge_required):
        base_price = min(0.999, max(0.0, float(live_quote['buy_price']) + ENTRY_PRICE_BUFFER))
        if not signal.get('aggressive', False):
            return base_price

        best_ask = max(0.0, _to_float(live_quote.get('best_ask'), 0.0))
        reprice_count = int(signal.get('reprice_count', 0) or 0)
        extra_step = AGGRESSIVE_PRICE_BUFFER + (reprice_count * AGGRESSIVE_REPRICE_STEP)
        aggressive_price = max(base_price, best_ask + extra_step if best_ask > 0 else base_price + extra_step)

        # Агрессивный режим должен ускорять fill, но не съедать весь edge.
        edge_guard = float(signal.get('model_prob', 0.0) or 0.0) - POLYMARKET_COMMISSION - min_edge_required - 0.005
        if edge_guard > 0:
            aggressive_price = min(aggressive_price, edge_guard)

        return min(max_price_allowed, min(0.999, max(base_price, aggressive_price)))

    def _try_ladder_strike(
        self,
        city,
        markets,
        event_date_str,
        temp,
        is_tail,
        peak_temp,
        gfs,
        bankroll,
        size_multiplier,
        confidence,
        risk_tier,
        stats,
    ):
        """
        Одна температура лестницы (YES). Возвращает item-dict или None.
        Не валит весь город при сбое — только пропускает страйк.
        """
        dist = abs(temp - peak_temp)
        stake_sigma = 1.0
        weight = math.exp(-0.5 * (dist / stake_sigma) ** 2)
        stake_preview = bankroll * MAX_POSITION_PCT * size_multiplier * weight
        stake_preview = max(MIN_BET_USD, min(stake_preview, MAX_BET_USD))

        temp_key = (city.lower(), int(round(temp)), event_date_str[:10])
        if temp_key in self.purchased_cache:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail уже в cache — пропускаем только tail")
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Страйк уже в cache — пропускаем")
            return None

        target = None
        for m in markets:
            q = m.get('question', '').lower()
            if f"{temp}°c" in q or f"{temp} c" in q:
                target = m
                break

        if not target:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Нет tail рынка — пропускаем только tail")
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Нет рынка для страйка — пропускаем")
                stats['skipped_liq'] += 1
            return None

        token_id = extract_market_token_id(target, outcome_name="Yes")
        if not token_id:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Нет token_id для tail — пропускаем tail")
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Нет token_id — пропускаем страйк")
                stats['skipped_liq'] += 1
            return None

        if str(token_id) in self.purchased_token_cache:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail token уже в token-cache — пропускаем tail")
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: token уже в token-cache — пропускаем")
                stats['skipped_city'] += 1
            return None

        fallback_price = float(target.get('lastTradePrice', target.get('price', 0.5)))
        estimated_contracts = max(1, round(stake_preview / max(fallback_price, 0.01)))
        quote = self.get_cached_quote(
            token_id,
            contracts=estimated_contracts,
            fallback_price=fallback_price,
            market=target,
        )

        market_price = min(0.999, max(0.0, quote['buy_price'] + ENTRY_PRICE_BUFFER))

        if market_price < MIN_CONTRACT_PRICE:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail слишком дешёвый (${market_price:.4f}) — пропускаем tail")
                stats['skipped_price'] += 1
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Цена ${market_price:.4f} < мин — пропускаем страйк")
                stats['skipped_price'] += 1
            return None

        max_price = self.get_entry_price_limit(risk_tier, confidence, is_tail=is_tail)
        if market_price >= max_price:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail ${market_price:.4f} > ${max_price:.4f} — пропускаем tail")
                stats['skipped_price'] += 1
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Цена ${market_price:.4f} > макс ${max_price:.4f} — пропускаем страйк")
                stats['skipped_price'] += 1
            return None

        sigma = gfs.get('sigma', GFS_STD)
        model_count = gfs.get('model_count', 1)
        hist_stats, _, _ = self.get_bias_stats(city, gfs.get('hours_to_resolution'))
        model_prob = self.estimate_bin_probability(
            temp,
            peak_temp,
            sigma,
            model_count,
            gfs.get('confidence', confidence),
            gfs.get('hours_to_resolution'),
            hist_stats=hist_stats,
        )

        min_prob = TAIL_MIN_PROB if is_tail else 0.03
        if model_prob < min_prob:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail prob {model_prob:.3f} < {min_prob:.3f} — пропускаем tail")
                stats['skipped_edge'] += 1
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Prob {model_prob:.3f} < {min_prob:.3f} — пропускаем страйк")
                stats['skipped_edge'] += 1
            return None

        effective_price = market_price + POLYMARKET_COMMISSION
        edge = model_prob - effective_price

        min_edge_required = TAIL_MIN_EDGE if is_tail else MIN_EDGE
        abs_gap = model_prob - effective_price
        pass_absolute = (
            market_price < EDGE_ABSOLUTE_PRICE_THRESHOLD
            and abs_gap >= EDGE_ABSOLUTE_MIN
        )
        if not pass_absolute and edge < min_edge_required:
            if is_tail:
                logger.info(f"   ⏭️ {city} {temp}°C: Tail edge {edge:.3f} < {min_edge_required:.3f} — пропускаем tail")
                stats['skipped_edge'] += 1
            else:
                logger.info(f"   ⏭️ {city} {temp}°C: Edge {edge:.3f} < {min_edge_required:.3f} — пропускаем страйк")
                stats['skipped_edge'] += 1
            return None

        return {
            'temp': temp,
            'target': target,
            'token_id': token_id,
            'market_price': market_price,
            'book_quote': quote,
            'model_prob': model_prob,
            'edge': edge,
            'sigma': sigma,
            'is_tail': is_tail,
            'confidence': confidence,
            'risk_tier': risk_tier,
            'size_multiplier': size_multiplier,
            'max_price_allowed': max_price,
        }

    def _try_bucket_strike(
        self,
        city,
        market,
        event_date_str,
        kind,
        threshold_c,
        peak_temp,
        gfs,
        bankroll,
        size_multiplier,
        confidence,
        risk_tier,
        stats,
    ):
        """Один бакетный рынок (or higher / or below). temp в cache — закодированный int."""
        encoded = self._encode_bucket_purchase_temp(kind, threshold_c)
        temp_key = (city.lower(), encoded, event_date_str[:10])
        if temp_key in self.purchased_cache:
            logger.info(f"   ⏭️ {city} bucket {kind} {threshold_c}°C: уже в cache")
            return None

        token_id = extract_market_token_id(market, outcome_name="Yes")
        if not token_id:
            logger.info(f"   ⏭️ {city} bucket: нет token_id — пропуск")
            stats['skipped_liq'] += 1
            return None
        if str(token_id) in self.purchased_token_cache:
            logger.info(f"   ⏭️ {city} bucket: token в cache — пропуск")
            stats['skipped_city'] += 1
            return None

        ref_peak = float(peak_temp)
        _cap_b = tail_max_dist_c_for_city(city)
        if _cap_b > 0 and abs(float(threshold_c) - ref_peak) > _cap_b:
            logger.info(
                f"   ⏭️ {city} bucket: |{threshold_c}−{ref_peak:.0f}|°C > max {_cap_b}°C — пропуск (tail cap)"
            )
            stats["skipped_edge"] += 1
            return None

        dist = abs(float(threshold_c) - ref_peak)
        stake_sigma = 1.0
        weight = math.exp(-0.5 * (dist / stake_sigma) ** 2)
        stake_preview = bankroll * MAX_POSITION_PCT * size_multiplier * weight
        stake_preview = max(MIN_BET_USD, min(stake_preview, MAX_BET_USD))

        fallback_price = float(market.get('lastTradePrice', market.get('price', 0.5)))
        estimated_contracts = max(1, round(stake_preview / max(fallback_price, 0.01)))
        quote = self.get_cached_quote(
            token_id,
            contracts=estimated_contracts,
            fallback_price=fallback_price,
            market=market,
        )
        market_price = min(0.999, max(0.0, quote['buy_price'] + ENTRY_PRICE_BUFFER))

        if market_price < MIN_CONTRACT_PRICE:
            logger.info(f"   ⏭️ {city} bucket: цена ${market_price:.4f} < мин — пропуск")
            stats['skipped_price'] += 1
            return None

        is_tail = abs(int(threshold_c) - int(round(peak_temp))) > CORE_LADDER_RADIUS
        max_price = self.get_entry_price_limit(risk_tier, confidence, is_tail=is_tail)
        if market_price >= max_price:
            logger.info(f"   ⏭️ {city} bucket: ${market_price:.4f} > макс ${max_price:.4f} — пропуск")
            stats['skipped_price'] += 1
            return None

        sigma = gfs.get('sigma', GFS_STD)
        if kind == "higher":
            model_prob = self.model_prob_bucket_or_higher(threshold_c, ref_peak, sigma)
        else:
            model_prob = self.model_prob_bucket_or_below(threshold_c, ref_peak, sigma)

        min_prob = TAIL_MIN_PROB if is_tail else 0.03
        if model_prob < min_prob:
            logger.info(f"   ⏭️ {city} bucket: prob {model_prob:.3f} < {min_prob:.3f} — пропуск")
            stats['skipped_edge'] += 1
            return None

        effective_price = market_price + POLYMARKET_COMMISSION
        edge = model_prob - effective_price
        min_edge_required = (TAIL_MIN_EDGE if is_tail else MIN_EDGE) * max(BUCKET_MIN_EDGE_MULT, 0.01)
        abs_gap = model_prob - effective_price
        pass_absolute = (
            market_price < EDGE_ABSOLUTE_PRICE_THRESHOLD
            and abs_gap >= EDGE_ABSOLUTE_MIN
        )
        if not pass_absolute and edge < min_edge_required:
            logger.info(f"   ⏭️ {city} bucket: edge {edge:.3f} < {min_edge_required:.3f} — пропуск")
            stats['skipped_edge'] += 1
            return None

        label = f"{'≥' if kind == 'higher' else '≤'}{threshold_c}°C"
        return {
            'temp': encoded,
            'threshold_c': int(threshold_c),
            'target': market,
            'token_id': token_id,
            'market_price': market_price,
            'book_quote': quote,
            'model_prob': model_prob,
            'edge': edge,
            'sigma': sigma,
            'is_tail': is_tail,
            'is_bucket': True,
            'bucket_kind': kind,
            'signal_label': label,
            'confidence': confidence,
            'risk_tier': risk_tier,
            'size_multiplier': size_multiplier,
            'max_price_allowed': max_price,
        }

    def maybe_build_no_signal(self, city, markets, event_date_str, peak_temp, gfs, confidence, risk_tier, size_multiplier, active_positions, bankroll):
        if not ENABLE_NO_OVERHEAT or confidence < NO_MIN_CONFIDENCE:
            return None

        max_no_price = self.get_no_price_limit(risk_tier, confidence)
        if max_no_price <= 0:
            return None

        sigma = gfs.get('sigma', GFS_STD)
        model_count = gfs.get('model_count', 1)
        hours_to_resolution = gfs.get('hours_to_resolution')
        hist_stats, _, _ = self.get_bias_stats(city, hours_to_resolution)
        best_candidate = None

        for market in markets:
            question = str(market.get('question', ''))
            match = re.search(r'(-?\d+)\s*°?\s*c', question, re.IGNORECASE)
            if not match:
                continue

            temp = int(match.group(1))
            purchase_key = (city.lower(), temp, event_date_str[:10])
            if purchase_key in self.purchased_cache:
                continue

            no_token_id = extract_market_token_id(market, outcome_name="No")
            if not no_token_id or str(no_token_id) in self.purchased_token_cache:
                continue

            yes_meta = get_market_metadata_quote(market, outcome_name="Yes")
            yes_price_hint = _to_float(
                yes_meta.get("outcome_price"),
                _to_float(market.get('lastTradePrice', market.get('price', 0.0)), 0.0)
            )
            if yes_price_hint < NO_MIN_YES_PRICE:
                continue

            yes_prob = self.estimate_bin_probability(
                temp,
                peak_temp,
                sigma,
                model_count,
                confidence,
                hours_to_resolution,
                hist_stats=hist_stats,
            )
            no_prob = max(0.0, min(1.0, 1.0 - yes_prob))
            if yes_prob > NO_MAX_YES_PROB or no_prob < NO_MIN_PROB:
                continue

            # Не шортим самый вероятный exact-исход, если модель всё ещё даёт ему заметный шанс.
            if abs(temp - peak_temp) <= 0 and yes_prob > 0.10:
                continue

            stake = bankroll * MAX_POSITION_PCT * size_multiplier * 0.75
            stake = max(MIN_BET_USD, min(stake, MAX_BET_USD))
            fallback_no_price = max(0.01, min(0.99, 1.0 - yes_price_hint))
            estimated_contracts = max(1, round(stake / max(fallback_no_price, 0.01)))
            quote = self.get_cached_quote(
                no_token_id,
                contracts=estimated_contracts,
                fallback_price=fallback_no_price,
                market=market,
                outcome_name="No",
            )
            no_price = min(0.999, max(0.0, quote['buy_price'] + ENTRY_PRICE_BUFFER))
            if no_price >= max_no_price:
                continue

            no_edge = no_prob - (no_price + POLYMARKET_COMMISSION)
            if no_edge < NO_MIN_EDGE:
                continue

            allowed, _ = self.check_limits(city, event_date_str, stake, active_positions, target_temp=temp)
            if not allowed:
                continue

            candidate = {
                'market': market,
                'city': city,
                'temp': temp,
                'price': no_price,
                'edge': no_edge,
                'model_prob': no_prob,
                'weight': 0.75,
                'date': event_date_str,
                'stake': stake,
                'aggressive': False,
                'is_tail': False,
                'is_no_hedge': True,
                'confidence': confidence,
                'risk_tier': risk_tier,
                'size_multiplier': size_multiplier,
                'token_id': no_token_id,
                'purchase_key': purchase_key,
                'reprice_count': self.pending_reprice_budget.get(purchase_key, 0),
                'max_price_allowed': max_no_price,
                'outcome_name': 'No',
                'side_label': 'NO',
            }
            score = (no_edge, no_prob, -no_price)
            if best_candidate is None or score > best_candidate[0]:
                best_candidate = (score, candidate)

        return best_candidate[1] if best_candidate else None

    def _cached_forecast_anchor_peak(self, city: str, event_date_yyyy_mm_dd: str, cache: dict) -> int | None:
        """Округлённый пик °C для (город, дата события); кэш на один проход выходов."""
        c = (city or "").lower()
        d = (event_date_yyyy_mm_dd or "")[:10]
        if not c or len(d) < 10:
            return None
        key = (c, d)
        if key in cache:
            return cache[key]
        real = self.get_gfs_forecast(c, d, track_history=False)
        if not real:
            cache[key] = None
            return None
        peak = float(real.get("peak", 0))
        peak, _ = self.apply_bias_correction(c, peak, real.get("hours_to_resolution"))
        a = int(round(peak))
        cache[key] = a
        return a

    def _is_forecast_anchor_exact_strike(self, ctx: dict, anchor_peak_cache: dict) -> bool:
        """Exact strike = round(peak)±EXIT_ANCHOR_RADIUS_C (та же геометрия, что у HOLD). Бакеты — нет."""
        if ctx.get("end_date") is None:
            return False
        t_raw = ctx.get("temp")
        if t_raw is None or is_bucket_encoded_temp(t_raw):
            return False
        try:
            strike_i = int(t_raw)
        except (TypeError, ValueError):
            return False
        ed = ctx["end_date"].date().isoformat()
        peak_a = self._cached_forecast_anchor_peak(ctx["city"], ed, anchor_peak_cache)
        if peak_a is None:
            return False
        return abs(strike_i - peak_a) <= EXIT_ANCHOR_RADIUS_C

    def maybe_manage_open_positions(self, active_positions):
        """
        Консервативные автоматические выходы:
        - partial take-profit: PnL-порог ниже для не-якорных (TAKE_PROFIT_PCT_NON_ANCHOR), выше для exact≈peak
        - optional time-based exit, но только если позиция не в минусе
        - forecast/edge exits выключены по умолчанию и не трогают losers
        """
        summary = {
            'checked': 0,
            'actions': 0,
            'hot': False,
            'fast': False,
            'reasons': {},
            'skip_no_bid': 0,
            'skip_unprofitable': 0,
            'hold_forecast_anchor': 0,
            'skip_min_exit_notional': 0,
        }

        if not ENABLE_AUTO_EXITS:
            return summary

        now = datetime.now(timezone.utc)
        anchor_peak_cache: dict = {}
        print(f"\n{C.BOLD}📤 Auto Exit Check:{C.ENDC}")

        for pos in active_positions:
            ctx = self.parse_position_context(pos)
            if not ctx['city'] or ctx['temp'] is None or not ctx['token_id'] or ctx['size'] <= 0:
                continue

            summary['checked'] += 1
            quote = self.get_cached_quote(
                ctx['token_id'],
                contracts=max(1, int(round(ctx['size']))),
                fallback_price=float(pos.get('curPrice', 0) or 0),
                outcome_name=ctx.get('outcome_name', 'Yes'),
            )
            best_bid = quote.get('sell_price', 0.0)
            if best_bid <= 0:
                summary['skip_no_bid'] += 1
                continue

            if EXIT_HOLD_FORECAST_ANCHOR and ctx["end_date"] is not None:
                t_raw = ctx["temp"]
                if not is_bucket_encoded_temp(t_raw):
                    try:
                        strike_i = int(t_raw)
                    except (TypeError, ValueError):
                        strike_i = None
                    if strike_i is not None:
                        ed = ctx["end_date"].date().isoformat()
                        peak_a = self._cached_forecast_anchor_peak(ctx["city"], ed, anchor_peak_cache)
                        if (
                            peak_a is not None
                            and abs(strike_i - peak_a) <= EXIT_ANCHOR_RADIUS_C
                            and best_bid < EXIT_ANCHOR_BYPASS_BID
                        ):
                            summary["hold_forecast_anchor"] += 1
                            logger.info(
                                f"   🧷 HOLD до резолва (событие {ed}, peak≈{peak_a}°C, "
                                f"r={EXIT_ANCHOR_RADIUS_C}°C): {ctx['city']} {strike_i}°C "
                                f"— авто-SELL пропущен (bid {best_bid*100:.1f}¢)"
                            )
                            continue

            avg_price = ctx['avg_price']
            pnl_pct = ((best_bid - avg_price) / avg_price) if avg_price > 0 else 0.0
            is_profitable = pnl_pct > 0
            hours_left = None
            if ctx['end_date'] is not None:
                hours_left = (ctx['end_date'] - now).total_seconds() / 3600.0

            sell_fraction = 0.0
            reason = None
            reason_detail = ""
            price_level_tag = None

            # Этап C: take-profit по уровням best bid (не зависит от AUTO_EXIT_ONLY_IN_PROFIT)
            if (
                ENABLE_TAKE_PROFIT_PRICE_LEVELS
                and ENABLE_TAKE_PROFIT_EXIT
                and TAKE_PROFIT_PRICE_LEVELS
            ):
                for lvl in TAKE_PROFIT_PRICE_LEVELS:
                    tag = f"take_profit_px_{lvl:g}"
                    if best_bid >= lvl and not self.has_exit_action(ctx['token_id'], tag):
                        sell_fraction = max(0.1, min(1.0, TAKE_PROFIT_PRICE_LEVEL_SELL_FRACTION))
                        reason = "take_profit_price_level"
                        price_level_tag = tag
                        reason_detail = f"bid {best_bid*100:.1f}¢ >= {lvl*100:.1f}¢"
                        summary['hot'] = True
                        break

            if (
                not reason
                and TAKE_PROFIT_BID_VS_AVG_MULT > 0
                and avg_price > 0
                and ENABLE_TAKE_PROFIT_EXIT
                and best_bid >= TAKE_PROFIT_BID_VS_AVG_MULT * avg_price
                and not self.has_exit_action(ctx['token_id'], "take_profit_bid_vs_avg")
            ):
                sell_fraction = max(0.1, min(1.0, TAKE_PROFIT_PRICE_LEVEL_SELL_FRACTION))
                reason = "take_profit_bid_vs_avg"
                reason_detail = (
                    f"bid {best_bid*100:.1f}¢ >= {TAKE_PROFIT_BID_VS_AVG_MULT:.1f}× avg {avg_price*100:.1f}¢"
                )
                summary['hot'] = True

            if not reason and AUTO_EXIT_ONLY_IN_PROFIT and not is_profitable:
                summary['skip_unprofitable'] += 1
                continue

            anchor_exact = self._is_forecast_anchor_exact_strike(ctx, anchor_peak_cache)
            tp_pnl_pct = TAKE_PROFIT_PCT if anchor_exact else TAKE_PROFIT_PCT_NON_ANCHOR

            if (
                not reason
                and ENABLE_TAKE_PROFIT_EXIT
                and pnl_pct >= tp_pnl_pct
                and not self.has_exit_action(ctx['token_id'], "take_profit_partial")
            ):
                sell_fraction = max(0.1, min(1.0, TAKE_PROFIT_SELL_FRACTION))
                reason = "take_profit_partial"
                reason_detail = (
                    f"PnL {pnl_pct*100:.1f}% >= {tp_pnl_pct*100:.1f}% "
                    f"({'якорь exact' if anchor_exact else 'не-якорь'})"
                )
                summary['hot'] = True
            elif (
                not reason
                and ENABLE_TIME_EXIT
                and hours_left is not None
                and hours_left <= EXIT_CLOSE_HOURS
                and pnl_pct >= TIME_EXIT_MIN_PNL_PCT
            ):
                sell_fraction = 1.0
                reason = "time_exit"
                reason_detail = (
                    f"До резолва {hours_left:.1f}ч <= {EXIT_CLOSE_HOURS:.1f}ч "
                    f"и PnL {pnl_pct*100:.1f}% >= {TIME_EXIT_MIN_PNL_PCT*100:.1f}%"
                )
                summary['hot'] = True
            elif (not reason) and (ENABLE_FORECAST_SHIFT_EXIT or ENABLE_EDGE_EXIT):
                real_gfs = self.get_gfs_forecast(ctx['city'], ctx['end_date'].isoformat() if ctx['end_date'] else now.isoformat(), track_history=False)
                if real_gfs:
                    current_peak = real_gfs.get('peak', ctx['temp'])
                    current_peak, _ = self.apply_bias_correction(ctx['city'], current_peak, real_gfs.get('hours_to_resolution'))
                    sigma = max(0.5, float(real_gfs.get('sigma', GFS_STD)))
                    model_count = real_gfs.get('model_count', 1)
                    hist_stats, _, _ = self.get_bias_stats(ctx['city'], real_gfs.get('hours_to_resolution'))
                    model_prob = self.estimate_bin_probability(
                        ctx['temp'],
                        current_peak,
                        sigma,
                        model_count,
                        real_gfs.get('confidence', 0.5),
                        real_gfs.get('hours_to_resolution'),
                        hist_stats=hist_stats,
                    )

                    if (
                        ENABLE_FORECAST_SHIFT_EXIT
                        and EXIT_OUTSIDE_CORE
                        and abs(ctx['temp'] - int(round(current_peak))) > CORE_LADDER_RADIUS
                    ):
                        sell_fraction = 1.0
                        reason = "forecast_shift"
                        reason_detail = f"Peak {current_peak}°C ушёл от позиции {ctx['temp']}°C"
                        summary['fast'] = True
                    elif ENABLE_EDGE_EXIT and model_prob < max(0.0, best_bid - EXIT_EDGE_BUFFER):
                        sell_fraction = 1.0
                        reason = "edge_exit"
                        reason_detail = f"Model {model_prob*100:.1f}% < bid {best_bid*100:.1f}%"
                        summary['fast'] = True

            if not reason or sell_fraction <= 0:
                continue

            try:
                sz = float(ctx.get("size") or 0)
            except Exception:
                sz = 0.0
            # Целые контракты по данным позиции; иначе max(1, floor(...)) просит 1 шт. при остатке 0.79 → 400 balance
            whole_shares = int(math.floor(sz + 1e-9))
            if whole_shares < 1:
                _tlab_d = format_purchase_temp_label(ctx["temp"])
                logger.info(
                    f"   ⏭️ SKIP exit (позиция < 1 контракта, size≈{sz:.4f}): "
                    f"{ctx['city']} {_tlab_d}"
                )
                continue

            contracts_to_sell = max(1, int(math.floor(sz * sell_fraction + 1e-9)))
            contracts_to_sell = min(contracts_to_sell, whole_shares)
            max_contracts = whole_shares
            if sell_fraction < 1.0 and MIN_EXIT_NOTIONAL_USD > 0 and best_bid > 0:
                need = int(math.ceil(MIN_EXIT_NOTIONAL_USD / best_bid))
                need = min(need, max_contracts)
                notional0 = best_bid * contracts_to_sell
                if notional0 < MIN_EXIT_NOTIONAL_USD:
                    contracts_to_sell = max(contracts_to_sell, need)
                if best_bid * contracts_to_sell + 1e-12 < MIN_EXIT_NOTIONAL_USD:
                    summary['skip_min_exit_notional'] += 1
                    _tlab_skip = format_purchase_temp_label(ctx['temp'])
                    logger.info(
                        f"   ⏭️ SKIP partial exit (min ${MIN_EXIT_NOTIONAL_USD:.2f}): "
                        f"{ctx['city']} {_tlab_skip} — max ${best_bid * max_contracts:.2f} < min"
                    )
                    continue

            result = place_sell_order(ctx['token_id'], best_bid, contracts_to_sell, self.client)
            _tlab = format_purchase_temp_label(ctx['temp'])
            evt_date = ctx["end_date"].date().isoformat() if ctx["end_date"] else "—"
            side_tag = str(ctx.get("outcome_name", "Yes")).upper()

            if result.get("status") == "ERROR":
                err = result.get("reason", "?")
                logger.error(f"   ❌ Exit error: {err}")
                print(
                    f"   ❌ {ctx['city'].upper()} {_tlab} | {reason} | "
                    f"SELL не прошёл ({contracts_to_sell} контр. @ {best_bid*100:.1f}¢)"
                )
                send_telegram(
                    f"<b>❌ WEATHER BOT v3.9 — SELL ERROR</b>\n"
                    f"\n"
                    f"<b>{ctx['city'].upper()}</b> | {_tlab} | {side_tag}\n"
                    f"📅 {evt_date}\n"
                    f"📌 {reason}: {reason_detail}\n"
                    f"⚠️ {err}\n"
                    f"📊 PnL (оценка): {pnl_pct * 100:+.1f}%"
                )
                continue

            summary['actions'] += 1
            summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
            self.mark_exit_action(ctx['token_id'], price_level_tag if price_level_tag else reason)
            self.write_telemetry("exit_signal", {
                'city': ctx['city'],
                'temp': ctx['temp'],
                'token_id': ctx['token_id'],
                'reason': reason,
                'reason_detail': reason_detail,
                'best_bid': best_bid,
                'contracts': contracts_to_sell,
                'avg_price': avg_price,
                'pnl_pct': pnl_pct,
            })

            print(f"   {'🧪' if DRY_RUN else '✅'} {ctx['city'].upper()} {_tlab} | {reason} | {contracts_to_sell} контр. @ {best_bid*100:.1f}¢")
            logger.info(f"   📤 EXIT {ctx['city']} {_tlab} — {reason}: {reason_detail}")

            proceeds = float(result.get("proceeds", round(best_bid * contracts_to_sell, 2)))
            cost_slice = avg_price * contracts_to_sell if avg_price > 0 else None
            pnl_slice_usd = (proceeds - cost_slice) if cost_slice is not None else None
            st = result.get("status")
            if st in ("OK", "DRY_RUN"):
                mode_hdr = "🧪 SELL [DRY_RUN]" if st == "DRY_RUN" else "✅ SELL [LIVE]"
                pnl_body = f"📊 PnL (к avg входу): <b>{pnl_pct * 100:+.1f}%</b>"
                if pnl_slice_usd is not None:
                    pnl_body += f"\n💹 PnL сделки (≈, до комиссий): <b>${pnl_slice_usd:+.2f}</b>"
                msg = (
                    f"<b>📤 WEATHER BOT v3.9 — {mode_hdr}</b>\n"
                    f"\n"
                    f"<b>{ctx['city'].upper()}</b> | {_tlab} | {side_tag}\n"
                    f"📅 Дата: {evt_date}\n"
                    f"📌 Выход: {reason}\n"
                    f"📝 {reason_detail}\n"
                    f"💰 Bid {best_bid * 100:.1f}¢ | Avg {avg_price * 100:.1f}¢\n"
                    f"📦 Контрактов: {contracts_to_sell}\n"
                    f"💵 Выручка (≈): ${proceeds:.2f}\n"
                    f"{pnl_body}\n"
                    f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
                )
                send_telegram(msg)

            if result.get("status") in ("OK", "DRY_RUN") and contracts_to_sell >= max_contracts:
                dkey = ctx["end_date"].date().isoformat() if ctx.get("end_date") else None
                if ctx.get("city") and ctx.get("temp") is not None and dkey:
                    self.remove_purchase_record(ctx["city"], ctx["temp"], dkey, ctx.get("token_id"))

        if summary.get("hold_forecast_anchor", 0):
            print(
                f"   🧷 Якорь до резолва (без авто-SELL): {summary['hold_forecast_anchor']} поз. "
                f"(peak GFS ±{EXIT_ANCHOR_RADIUS_C}°C, bypass bid≥{EXIT_ANCHOR_BYPASS_BID*100:.0f}¢)"
            )

        if summary['actions'] == 0:
            parts = [f"проверено позиций: {summary['checked']}"]
            if summary.get('skip_no_bid', 0):
                parts.append(f"без bid: {summary['skip_no_bid']}")
            if summary.get('skip_unprofitable', 0):
                parts.append(f"только в плюс (skip): {summary['skip_unprofitable']}")
            if summary.get("hold_forecast_anchor", 0):
                parts.append(f"якорь до резолва (hold): {summary['hold_forecast_anchor']}")
            if summary.get("skip_min_exit_notional", 0):
                parts.append(
                    f"частичный < ${MIN_EXIT_NOTIONAL_USD:.2f}: {summary['skip_min_exit_notional']}"
                )
            print(f"   Нет сигналов на выход ({'; '.join(parts)})")

        return summary

    def robust_fetch(self, url):
        """
        Пробует скачать URL несколькими способами, чтобы обойти SSL ошибки Windows.
        """
        # 1. Пробуем HTTP напрямую (без SSL)
        http_url = url.replace("https://", "http://")
        try:
            res = requests.get(http_url, verify=False, timeout=15)
            if res.ok:
                return res.json()
        except:
            pass
        
        # 2. Пробуем HTTPS
        try:
            res = requests.get(url, verify=False, timeout=15)
            if res.ok:
                return res.json()
        except:
            pass
        
        # 3. Fallback: curl с binary mode
        try:
            result = subprocess.run(
                ['curl', '-k', '-s', '-L', http_url],
                capture_output=True,
                timeout=15,
                encoding='utf-8',
                errors='replace'  # Заменяем невалидные символы
            )
            if result.returncode == 0 and result.stdout:
                return json.loads(result.stdout)
        except:
            pass
        
        return None

    def _gamma_fetch_slug_events(self, slug: str):
        """Один slug → список событий или []. Для параллельного опроса Gamma API."""
        try:
            url = f"{self.api_url}?slug={slug}"
            r = requests.get(url, timeout=GAMMA_SLUG_FETCH_TIMEOUT_SEC, verify=False)
            if r.status_code == 200:
                result = r.json()
                if result and len(result) > 0:
                    return result
        except Exception:
            pass
        return []

    def check_limits(self, city, event_date_str, potential_stake, active_positions, target_temp=None):
        """
        Проверяет лимиты ТОЛЬКО по активным позициям.
        Также проверяет дубликаты (тот же город + дата + температура).
        Возвращает (allowed: bool, reason: str)
        """
        try:
            # Парсим дату события
            if 'T' in event_date_str:
                event_date = datetime.fromisoformat(event_date_str.split('.')[0].replace('Z', ''))
            else:
                event_date = datetime.strptime(event_date_str.split(' ')[0], "%Y-%m-%d")
        except:
            return False, "Bad Date Format"

        # Считаем текущую экспозицию
        current_city_exposure = 0.0
        current_date_exposure = 0.0

        for pos in active_positions:
            try:
                p_date_str = pos.get('endDate', '')
                if 'T' in p_date_str:
                    p_date = datetime.fromisoformat(p_date_str.split('.')[0].replace('Z', ''))
                else:
                    p_date = datetime.strptime(p_date_str.split(' ')[0], "%Y-%m-%d")

                # Грубый поиск города в названии актива
                p_title = pos.get('title', '').lower()
                is_same_city = city.lower() in p_title

                same_event_date = abs((p_date - event_date).total_seconds()) < 43200

                if is_same_city and same_event_date:
                    # Оценка текущих вложений (avgPrice * size)
                    val = float(pos.get('avgPrice', 0)) * float(pos.get('size', 0))
                    current_city_exposure += val

                    # 🔒 ПРОВЕРКА ДУБЛИКАТА: тот же город + дата + температура
                    if target_temp is not None:
                        import re
                        pos_match = re.search(r'(-?\d+)\s*°?C', pos.get('title', ''))
                        if pos_match:
                            pos_temp = int(pos_match.group(1))
                            # Та же дата (±12ч) + та же температура = ДУБЛИКАТ
                            if same_event_date and pos_temp == target_temp:
                                return False, f"Дубликат: {city} {target_temp}°C уже есть"

                # Проверка на ту же дату (плюс-минус 12 часов)
                if same_event_date:
                    val = float(pos.get('avgPrice', 0)) * float(pos.get('size', 0))
                    current_date_exposure += val
            except:
                continue

        # Лимит события (город+дата): по умолчанию MAX_EXPOSURE_PER_EVENT_PCT (= city pct, если не задан отдельно)
        risk_base = float(getattr(self, "_risk_bankroll", None) or BANKROLL)
        limit_city = risk_base * MAX_EXPOSURE_PER_EVENT_PCT
        if current_city_exposure + potential_stake > limit_city:
            return False, f"Event Limit (${current_city_exposure:.2f}/{limit_city:.2f})"
            
        # Лимит даты
        limit_date = risk_base * MAX_EXPOSURE_PER_DATE_PCT
        if current_date_exposure + potential_stake > limit_date:
            return False, f"Date Limit (${current_date_exposure:.2f}/{limit_date:.2f})"

        return True, "OK"

    def clean_old_purchases(self, active_positions):
        """Очищает cache от позиций которые уже закрылись (экспирация прошла)"""
        now = datetime.now(timezone.utc)
        keys_to_remove = set()
        
        for key in self.purchased_cache:
            city, temp, date_str = key
            # Проверяем есть ли ещё такая позиция в active_positions
            found = False
            for pos in active_positions:
                pos_title = pos.get('title', '').lower()
                if city in pos_title:
                    try:
                        end_str = pos.get('endDate', '')
                        if 'T' in end_str:
                            end_date = datetime.fromisoformat(end_str.split('.')[0].replace('Z', ''))
                        else:
                            end_date = datetime.strptime(end_str.split(' ')[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if end_date > now:
                            found = True
                            break
                    except:
                        pass
            
            if not found:
                # Позиция закрылась — можно удалить из cache
                keys_to_remove.add(key)
        
        for key in keys_to_remove:
            self.purchased_cache.discard(key)
            logger.debug(f"   🧹 Удалено из cache (позиция закрыта): {key}")

    def calculate_gfs(self, outcomes, real_gfs=None, city=None):
        """
        📉 MARKET ANALYSIS + REAL GFS INTEGRATION:
        1. Парсит температуры и цены рынков
        2. Если есть реальный GFS forecast → используем его как peak
        3. Иначе fallback на market consensus (старое поведение)
        4. Удаляет аутлаеры цен
        5. Строит ladder вокруг GFS peak
        """
        if not outcomes:
            return None

        temps = []
        probs = []

        for out in outcomes:
            title = out.get('question', '')
            price = float(out.get('lastTradePrice', out.get('price', 0)))

            # Парсинг "XX°C" или "XX C"
            temp_val = None
            import re
            match = re.search(r'(\d+)\s*°?C', title)
            if match:
                temp_val = int(match.group(1))

            if temp_val is not None:
                temps.append(temp_val)
                probs.append(price)

        if not temps:
            return None

        df = pd.DataFrame({'temp': temps, 'prob': probs})
        df = df.sort_values('temp').reset_index(drop=True)

        # OUTLIER REMOVAL цен рынков
        n = len(df)
        if n > 5:
            trim_count = max(1, int(n * GFS_OUTLIER_TRIM))
            df = df.iloc[trim_count : n - trim_count]

        # Расчет
        total_prob = df['prob'].sum()
        if total_prob > 0:
            expected_temp = (df['temp'] * df['prob']).sum() / total_prob
            df['prob_norm'] = df['prob'] / total_prob
        else:
            expected_temp = df['temp'].mean()
            df['prob_norm'] = 1/len(df)

        # v3.9: РЕАЛЬНЫЙ GFS PEAK (не market consensus!)
        if real_gfs and 'peak' in real_gfs:
            peak_temp = real_gfs['peak']
            logger.info(f"   🌡️ GFS Real Forecast: {real_gfs.get('forecast_max', peak_temp):.1f}°C → peak={peak_temp}°C")
        else:
            # Fallback: market consensus
            if not df.empty:
                peak_temp = df.loc[df['prob'].idxmax(), 'temp']
            else:
                return None
            logger.warning(f"   ⚠️ GFS fallback: market consensus peak={peak_temp}°C")

        sigma = real_gfs.get('sigma', GFS_STD) if real_gfs else GFS_STD
        model_count = real_gfs.get('model_count', 1) if real_gfs else 1
        confidence = real_gfs.get('confidence', 0.50) if real_gfs else 0.50
        risk_tier = real_gfs.get('risk_tier', 'unknown') if real_gfs else 'unknown'
        size_multiplier = real_gfs.get('size_multiplier', 0.25) if real_gfs else 0.25
        tails_allowed = real_gfs.get('tails_allowed', False) if real_gfs else False
        spread = float(real_gfs.get("spread", 0.0) or 0.0) if real_gfs else 0.0

        # Лестница вокруг GFS peak
        ladder, core_temps, tail_temps = self.build_ladder(peak_temp, df["temp"].values, sigma=sigma, city=city)

        return {
            'mean': expected_temp,
            'median': peak_temp,  # Теперь это GFS peak!
            'ladder': ladder,
            'core_temps': core_temps,
            'tail_temps': tail_temps,
            'df': df,
            'sigma': sigma,
            'model_count': model_count,
            'confidence': confidence,
            'risk_tier': risk_tier,
            'size_multiplier': size_multiplier,
            'tails_allowed': tails_allowed,
            'spread': spread,
            'source': 'gfs' if real_gfs else 'market',
            'bucket_only': False,
        }

    def calc_bucket_probability(self, temp, peak, sigma):
        """
        📐 CDF-based bucket probability: P(temp - 0.5 < X < temp + 0.5)
        Намного точнее чем PDF * 1°C, особенно на хвостах распределения.
        """
        # CDF нормального распределения через math.erf
        def cdf(x):
            return 0.5 * (1 + math.erf((x - peak) / (sigma * math.sqrt(2))))
        
        cdf_upper = cdf(temp + 0.5)
        cdf_lower = cdf(temp - 0.5)
        return cdf_upper - cdf_lower

    def build_weighted_ladder(self, peak, all_temps, sigma):
        """
        v3.9: DYNAMIC LADDER на основе σ (не фиксированные ±10°C!)
        Если σ маленькая — узкий ladder (ядро). Если большая — широкий.
        """
        temps_sorted = sorted(list(set(all_temps)))
        if peak not in temps_sorted:
            return [peak], {int(peak): 1.0}

        idx = temps_sorted.index(peak)
        
        # Dynamic range на основе sigma:
        # ±2σ покрывает 95% вероятности, ±3σ = 99.7%
        # Но не больше ±7°C (чтобы не покупать хвосты)
        dynamic_range = min(int(math.ceil(3 * sigma)), 7)
        dynamic_range = max(dynamic_range, 3)  # минимум ±3°C
        
        logger.info(f"   📏 Dynamic ladder: ±{dynamic_range}°C (σ={sigma:.2f})")
        
        ladder = [peak]
        weights = {peak: 1.0}  # Пик = 100% веса

        # Добавляем соседей с весами (Gaussian weighting)
        for i in range(1, dynamic_range + 1):
            # Вес уменьшается по Гауссу
            weight = math.exp(-0.5 * (i / sigma) ** 2)
            
            for direction in [-1, 1]:
                new_idx = idx + (direction * i)
                if 0 <= new_idx < len(temps_sorted):
                    val = temps_sorted[new_idx]
                    if val not in ladder:
                        ladder.append(val)
                        weights[val] = weight

        return sorted(list(set(ladder))), weights

    def build_ladder(self, peak, all_temps, sigma=None, city=None):
        """
        v4.2: 3-режимный adaptive ladder.
        - низкая sigma: только core
        - средняя sigma: core + cheap tails
        - высокая sigma: расширяем ladder до ±HIGH_LADDER_RADIUS
        """
        temps_sorted = sorted(list(set(int(t) for t in all_temps)))
        if not temps_sorted:
            peak = int(round(peak))
            return [peak], [peak], []

        # Если peak не совпал с доступной температурой рынка, привязываемся к ближайшей.
        nearest_peak = min(temps_sorted, key=lambda t: abs(t - peak))
        idx = temps_sorted.index(nearest_peak)

        core = []
        for offset in range(-CORE_LADDER_RADIUS, CORE_LADDER_RADIUS + 1):
            new_idx = idx + offset
            if 0 <= new_idx < len(temps_sorted):
                core.append(temps_sorted[new_idx])

        tails = []
        sigma_eff = (sigma * AI_LADDER_SIGMA_MULT) if (AI_WEATHER and sigma is not None) else sigma
        if TAIL_ENABLED and sigma_eff is not None:
            if sigma_eff >= HIGH_SIGMA_THRESHOLD:
                tail_radius = max(TAIL_LADDER_RADIUS, HIGH_LADDER_RADIUS)
            elif sigma_eff >= max(TAIL_MIN_SIGMA, MID_SIGMA_THRESHOLD):
                tail_radius = TAIL_LADDER_RADIUS
            else:
                tail_radius = CORE_LADDER_RADIUS

            tail_start = CORE_LADDER_RADIUS + 1
            for step in range(tail_start, tail_radius + 1):
                for direction in (-1, 1):
                    new_idx = idx + direction * step
                    if 0 <= new_idx < len(temps_sorted):
                        tails.append(temps_sorted[new_idx])

        core = sorted(list(set(core)))
        tails = sorted([t for t in set(tails) if t not in core])
        _cap = tail_max_dist_c_for_city(city)
        if _cap > 0:
            _before = len(tails)
            tails = [t for t in tails if abs(int(t) - int(nearest_peak)) <= _cap]
            if _before > len(tails):
                logger.info(
                    f"   🧷 Tail dist cap ±{_cap}°C от пика {nearest_peak}°C — "
                    f"убрано {_before - len(tails)} хвостов (city={city or '—'})"
                )
        ladder = sorted(list(set(core + tails)))
        if AI_WEATHER and len(ladder) > AI_LADDER_MAX_LEGS:
            by_dist = sorted(ladder, key=lambda t: abs(t - nearest_peak))
            ladder = sorted(by_dist[:AI_LADDER_MAX_LEGS])
            core = [t for t in core if t in ladder]
            tails = [t for t in tails if t in ladder]
        return ladder, core, tails

    def run(self):
        self.book_cache = {}
        self.pending_reprice_budget = {}
        self.pending_reprice_blocked = set()
        print(f"\n{C.BOLD}{'═'*60}{C.ENDC}")
        print(f"{C.BOLD}🌡️  WEATHER BOT v4.0  |  ANALYSIS-OPTIMIZED{C.ENDC}")
        print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"{'═'*60}")
        _mode = f"ai_weather (automatedAI-style)" if AI_WEATHER else f"legacy ({STRATEGY_MODE})"
        print(f"   Режим: {_mode} | часы до экспирации: {MIN_HOURS_TO_EXPIRY}–{MAX_HOURS_TO_EXPIRY} ч")
        print(f"   Стратегия: Ensemble (GFS+ECMWF+UKMO+BOM) + Ultra-Cheap + NO Hedge")
        if AI_WEATHER:
            _qspread = f"≤{MAX_ENSEMBLE_SPREAD_C:.2f}°C" if MAX_ENSEMBLE_SPREAD_C > 0 else "выкл."
            _bal = (
                f"ENTRY_QUALITY_BALANCE={ENTRY_QUALITY_BALANCE:.2f} (0=объём, 1=качество)"
                if ENTRY_QUALITY_BALANCE is not None
                else "ENTRY_QUALITY_BALANCE=off (ручные дефолты по ключам)"
            )
            print(
                f"   📌 Качество входа: {_bal}; conf≥{CONFIDENCE_SKIP_THRESHOLD:.2f}, "
                f"min edge {MIN_EDGE:.3f}, моделей≥{MIN_MODELS_FOR_ENTRY}, spread {_qspread}; "
                f"min event liquidity ${MIN_EVENT_LIQUIDITY_USD:.0f}"
            )
        if AUTO_SCAN:
            interval, status, _ = get_gfs_scan_interval()
            print(f"   🔄 Автоскан: каждые {interval//60} мин ({status})")
        if TG_ENABLED:
            print(f"   📱 Telegram: включён (уведомления о покупках/сделках)")
        if DRY_RUN: print(f"   {C.WARNING}⚠️  РЕЖИМ: DRY RUN (Симуляция){C.ENDC}")
        else: print(f"   {C.GREEN}🔴 РЕЖИМ: LIVE TRADING{C.ENDC}")
        if ENABLE_AUTO_REDEEM and not DRY_RUN and AUTO_REDEEM_MAX_PER_SCAN > 0:
            _w = "только weather в title" if AUTO_REDEEM_WEATHER_ONLY else "все redeemable"
            _cd = (
                f", пауза ≥{AUTO_REDEEM_MIN_INTERVAL_SEC}s между попытками"
                if AUTO_REDEEM_MIN_INTERVAL_SEC > 0
                else ""
            )
            _mp = (
                f", min оценка позиции ${AUTO_REDEEM_MIN_PAYOUT_USD:.2f}+"
                if AUTO_REDEEM_MIN_PAYOUT_USD > 0
                else ""
            )
            _age = (
                f", только рынок закончился ≤{AUTO_REDEEM_MAX_POSITION_AGE_DAYS} дн. назад"
                if AUTO_REDEEM_MAX_POSITION_AGE_DAYS > 0
                else ""
            )
            print(
                f"   💰 Auto-redeem: ON (≤{AUTO_REDEEM_MAX_PER_SCAN} tx/цикл{_cd}, {_w}{_mp}{_age})"
            )
        elif ENABLE_AUTO_REDEEM and DRY_RUN:
            print(f"   💰 Auto-redeem: выключен в DRY_RUN (включите LIVE)")
        elif ENABLE_AUTO_REDEEM and AUTO_REDEEM_MAX_PER_SCAN <= 0:
            print(
                f"   💰 Auto-redeem: OFF — {C.BOLD}AUTO_REDEEM_MAX_PER_SCAN=0{C.ENDC} "
                f"(задайте ≥1 в .env)"
            )
        else:
            print(
                f"   💰 Auto-redeem: OFF — в .env раскомментируйте и задайте "
                f"{C.BOLD}ENABLE_AUTO_REDEEM=true{C.ENDC} (не путать с ENABLE_AUTO_EXITS)"
            )
        if ENABLE_AUTO_DUMP_DEAD and AUTO_DUMP_DEAD_MAX_PER_SCAN > 0:
            _dw = "только weather в title" if AUTO_DUMP_DEAD_WEATHER_ONLY else "все рынки"
            _dcd = (
                f", пауза ≥{AUTO_DUMP_DEAD_MIN_INTERVAL_SEC}s"
                if AUTO_DUMP_DEAD_MIN_INTERVAL_SEC > 0
                else ""
            )
            _mn = (
                f", выручка≥${AUTO_DUMP_DEAD_MIN_NOTIONAL_USD:.2f}"
                if AUTO_DUMP_DEAD_MIN_NOTIONAL_USD > 0
                else ""
            )
            print(
                f"   🗑️ Auto-dump dead: ON (≤{AUTO_DUMP_DEAD_MAX_PER_SCAN} SELL/цикл{_dcd}, "
                f"cur≤{AUTO_DUMP_DEAD_MAX_CUR*100:.1f}¢, bid≥{AUTO_DUMP_DEAD_MIN_BID*100:.2f}¢{_mn}, {_dw})"
            )
        else:
            print(
                f"   🗑️ Auto-dump dead: OFF — {C.BOLD}ENABLE_AUTO_DUMP_DEAD=true{C.ENDC} "
                f"(слив прошлых не-redeemable позиций через CLOB)"
            )
        print(f"{'═'*60}\n")

        # 1. Загрузка данных
        log_blue("📥 Загрузка позиций и рынков...")

        bankroll, all_positions = self.get_balance_and_positions()
        if bankroll is None or all_positions is None:
            logger.error("   ⛔ Startup aborted: не удалось получить баланс/позиции Polymarket, trading cycle пропущен")
            return
        self._risk_bankroll = float(bankroll)
        self.reconcile_purchases_with_positions(all_positions)
        _pct = SESSION_BUDGET_PCT * 100.0
        print(
            f"   💵 USDC на кошельке: ${float(bankroll):.2f}  |  "
            f"лимит расхода за скан: ${self.session_budget:.2f} ({_pct:.0f}% от USDC, env SESSION_BUDGET_PCT)"
        )
        self.reconcile_forecast_learning(all_positions)
        self.maybe_auto_redeem_resolved(all_positions)
        self.maybe_auto_dump_dead_positions(all_positions)
        active_positions = self.get_active_positions(all_positions)

        # 🔧 v4.0.1: НЕ очищаем cache! 
        # Раньше clean_old_purchases удалял купленные позиции из cache,
        # и бот покупал ТО ЖЕ САМОЕ снова (Toronto 6 раз!)
        # Теперь cache — PERMANENT: купил = запомнил навсегда
        
        logger.info(f"   🛡️ Cache до загрузки: {len(self.purchased_cache)} позиций (НЕ очищаем!)")
        
        # 📥 ДОБАВЛЯЕМ ВСЕ ПОЗИЦИИ ИЗ POLYMARKET В CACHE (и active и expired!)
        # Это предотвращает покупку позиций которые уже есть на кошельке
        # Кешируем ВСЕ 37 позиций, не только active (2)
        positions_to_cache = all_positions  # Используем ВСЕ позиции
        
        for pos in positions_to_cache:
            try:
                pos_title = pos.get('title', '').lower()
                end_str = pos.get('endDate', '')

                # Извлекаем город
                city = resolve_trading_city_from_title(pos_title)
                if not city:
                    continue
                
                # Извлекаем температуру (только явный °C; °F не кешируем как °C)
                import re
                if "°f" in pos_title or "fahrenheit" in pos_title:
                    continue
                temp_match = re.search(r"(-?\d+)\s*°\s*c\b", pos_title)
                if not temp_match:
                    temp_match = re.search(r"(-?\d+)\s*°c\b", pos_title)
                if not temp_match:
                    continue
                temp = int(temp_match.group(1))
                
                # Извлекаем дату
                if 'T' in end_str:
                    end_date = datetime.fromisoformat(end_str.split('.')[0].replace('Z', ''))
                else:
                    end_date = datetime.strptime(end_str.split(' ')[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                
                date_str = end_date.strftime('%Y-%m-%d')
                
                # Добавляем в cache
                purchase_key = (city.lower(), temp, date_str)
                pos_token = str(pos.get('asset') or pos.get('asset_id') or pos.get('token_id') or '')
                if purchase_key not in self.purchased_cache:
                    self.purchased_cache.add(purchase_key)
                    size = pos.get('size', 0)
                    logger.info(f"   📥 Добавлено из Polymarket: {city.upper()} {temp}°C {date_str} (size: {size})")
                else:
                    logger.debug(f"   ⏭️ Уже в cache: {city.upper()} {temp}°C {date_str}")
                if pos_token:
                    self.purchased_token_cache.add(pos_token)
                    self.clear_pending_order(purchase_key=purchase_key, token_id=pos_token)
                self.save_purchase(city, temp, date_str, token_id=pos_token or None)
            except Exception as e:
                logger.warning(f"   ⚠️ Пропуск позиции: {e}, title: {pos.get('title', 'N/A')[:60]}")

        logger.info(f"   🛡️ Итого в cache: {len(self.purchased_cache)} позиций (не купим снова)")
        self.sync_pending_orders_from_api()
        self.manage_pending_orders()

        logger.info(f"   Всего позиций: {len(all_positions)}")
        logger.info(f"   Active (future): {len(active_positions)}")
        logger.info(f"   Ignored (expired): {len(all_positions) - len(active_positions)}")

        exit_summary = self.maybe_manage_open_positions(active_positions)

        try:
            # Ищем температурные события через events API + прямой поиск по slug
            # Polymarket API пагинирует события, температурные рынки могут быть далеко
            # Поэтому загружаем БОЛЬШЕ батчей + ищем напрямую по slug
            all_events = []

            # Батчи: загружаем глубже (до offset 2000)
            for offset in [0, 500, 1000, 1500, 2000]:
                url = f"{self.api_url}?closed=false&limit=500&offset={offset}&order=endDate&ascending=true"
                try:
                    batch = self.robust_fetch(url)
                    if batch:
                        all_events.extend(batch)
                        if len(batch) < 500:
                            break  # Больше нет событий
                except:
                    pass

            logger.info(f"   Загружено событий: {len(all_events)}")

            # Прямой поиск рынков по slug (April 5-11) — сегменты как на Polymarket (nyc, los-angeles, …)
            april_slugs = []
            for day in range(5, 12):  # April 5-11
                for slug_city in POLYMARKET_TEMPERATURE_SLUGS:
                    april_slugs.append(f"highest-temperature-in-{slug_city}-on-april-{day}-2026")

            logger.info(
                f"   Проверяем {len(april_slugs)} slug'ов "
                f"(параллельно, {GAMMA_SLUG_FETCH_WORKERS} потоков)..."
            )

            found_direct = 0
            if april_slugs:
                max_w = min(GAMMA_SLUG_FETCH_WORKERS, len(april_slugs))
                with ThreadPoolExecutor(max_workers=max_w) as pool:
                    for chunk in pool.map(self._gamma_fetch_slug_events, april_slugs):
                        if chunk:
                            all_events.extend(chunk)
                            found_direct += 1

            logger.info(f"   Найдено через прямой поиск: {found_direct}")
            
            if not all_events:
                logger.error("❌ Не удалось загрузить события!")
                return
            
            # Фильтруем только температурные события
            events = []
            temp_keywords = ['highest temperature']
            
            for event in all_events:
                title = event.get('title', '').lower()
                slug = event.get('slug', '').lower()
                
                # Проверяем: есть ли keyword температуры
                has_temp_keyword = any(k in title or k in slug for k in temp_keywords)
                
                if has_temp_keyword:
                    events.append(event)
            
            # Дедупликация по slug
            seen_slugs = set()
            unique_events = []
            for e in events:
                slug = e.get('slug', '')
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    unique_events.append(e)
            events = unique_events
            
            # 🔥 ФИЛЬТР ПРОШЕДШИХ РЫНКОВ — отсекаем ДО анализа
            now = datetime.now(timezone.utc)
            active_events = []
            skipped_expired = 0
            for e in events:
                end_str = e.get('endDateIso', e.get('endDate', ''))
                if not end_str:
                    skipped_expired += 1  # Нет даты = считаем прошедшим
                    continue
                try:
                    if 'T' in end_str:
                        end_date = datetime.fromisoformat(end_str.split('.')[0].replace('Z', '')).replace(tzinfo=timezone.utc)
                    else:
                        end_date = datetime.strptime(end_str.split(' ')[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    
                    hours_left = (end_date - now).total_seconds() / 3600
                    if hours_left > 0:
                        active_events.append(e)
                    else:
                        skipped_expired += 1
                except:
                    skipped_expired += 1
            
            events = active_events
            
            logger.info(f"   Активных рынков: {len(events)} ({skipped_expired} прошедших отфильтровано)")
            
            if not events:
                logger.warning("⚠️ Не найдено температурных событий!")
                logger.warning("   Возможно, Polymarket ещё не создал новые рынки")
                return
        except Exception as e:
            logger.error(f"   [error] fetch markets: {e}")
            return

        now = datetime.now(timezone.utc)
        signals = []
        
        # Счетчики для отладки (почему пропускаем?)
        stats = {
            'scanned': 0,
            'skipped_city': 0,
            'skipped_time': 0,
            'skipped_liq': 0,
            'skipped_gfs': 0,
            'skipped_stability': 0,
            'skipped_city_stoploss': 0,  # v3.9: city stop-loss
            'skipped_edge': 0,
            'skipped_price': 0,
            'signals': 0,
            'gfs_real': 0,  # v3.9: реальный GFS
            'gfs_fallback': 0,  # v3.9: fallback на market
            'bias_corrected': 0,  # v4.0: GFS bias correction applied
        }
        self._scan_min_hours_to_expiry = None

        # 2. Анализ
        log_blue("🔍 Анализ рынков...")
        
        for event in events:
            stats['scanned'] += 1
            blocked_soft = False

            title = event.get('title', '').lower()
            city = resolve_trading_city_from_title(title)
            if not city:
                stats['skipped_city'] += 1
                continue
            
            markets = event.get('markets', [])
            if not markets:
                continue

            exact_markets = [m for m in markets if self.is_exact_temperature_market(m.get('question', ''))]
            bucket_markets = [m for m in markets if self.classify_bucket_market(m.get('question', ''))]

            # Дата экспирации
            end_str = event.get('endDateIso', event.get('endDate', ''))
            if not end_str: continue
            
            try:
                if 'T' in end_str:
                    end_date = datetime.fromisoformat(end_str.split('.')[0].replace('Z', ''))
                    # Делаем timezone-aware
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                else:
                    end_date = datetime.strptime(end_str.split(' ')[0], "%Y-%m-%d")
                    end_date = end_date.replace(tzinfo=timezone.utc)

                hours_left = (end_date - now).total_seconds() / 3600
                if hours_left < MIN_HOURS_TO_EXPIRY or hours_left > MAX_HOURS_TO_EXPIRY:
                    stats['skipped_time'] += 1
                    logger.info(f"   ⏭️ {event.get('title')[:60]}: hours_left={hours_left:.1f} (вне диапазона {MIN_HOURS_TO_EXPIRY}-{MAX_HOURS_TO_EXPIRY})")
                    continue

                event_date_str = end_date.isoformat()
                if self._scan_min_hours_to_expiry is None:
                    self._scan_min_hours_to_expiry = hours_left
                else:
                    self._scan_min_hours_to_expiry = min(self._scan_min_hours_to_expiry, hours_left)
            except: continue

            # v3.9: BLOCKED CITY CHECK (исторически плохие города)
            if city.lower() in BLOCKED_CITIES:
                if BLOCKED_CITIES_MODE == "soft":
                    blocked_soft = True
                    logger.info(
                        f"   ⚠️ {city.upper()}: BLOCKED_CITIES soft — stake × {BLOCKED_CITIES_SOFT_STAKE_MULT}"
                    )
                else:
                    stats['skipped_city'] += 1
                    logger.info(f"   🚫 {city.upper()}: заблокирован (0% WinRate исторически)")
                    continue

            # Liquidity Filter (Защита от мусора / слишком тонкого стакана)
            liquidity = float(event.get('liquidity', 0))
            if liquidity < MIN_EVENT_LIQUIDITY_USD:
                stats['skipped_liq'] += 1
                logger.debug(
                    f"   ⏭️ {event.get('title', '')[:50]}: liquidity ${liquidity:.0f} < ${MIN_EVENT_LIQUIDITY_USD:.0f} — пропуск"
                )
                continue

            # v3.9: CITY STOP-LOSS CHECK
            blocked, block_reason = self.check_city_stoploss(city)
            if blocked:
                stats['skipped_city_stoploss'] += 1
                logger.info(f"   🚫 {city.upper()}: {block_reason}")
                continue

            # v3.9: РЕАЛЬНЫЙ GFS FORECAST (Open-Meteo API)
            real_gfs = self.get_gfs_forecast(city, event_date_str, track_history=True)

            if exact_markets:
                gfs = self.calculate_gfs(exact_markets, real_gfs=real_gfs, city=city)
            else:
                gfs = None

            if gfs is None and ENABLE_BUCKET_MARKETS and bucket_markets:
                gfs = self.gfs_dict_from_real_forecast(real_gfs)
            elif gfs is None:
                stats['skipped_gfs'] += 1
                continue
            
            # Track GFS source
            if gfs.get('source') == 'gfs':
                stats['gfs_real'] += 1
            else:
                stats['gfs_fallback'] += 1
                stats['skipped_gfs'] += 1
                logger.warning(f"   ⛔ {city}: нет real forecast, market-fallback для live entry запрещён — пропускаем")
                self.write_telemetry("entry_skip", {
                    'city': city,
                    'date': event_date_str[:10],
                    'reason': 'missing_real_forecast',
                    'fallback_source': gfs.get('source'),
                })
                continue

            # 🌪️ GFS STABILITY CHECK (сравниваем реальные GFS прогнозы!)
            gfs_peak_raw = gfs['median']

            # 🔧 GFS BIAS CORRECTION — корректируем на основе истории ошибок
            gfs_peak, bias_info = self.apply_bias_correction(city, gfs_peak_raw, gfs.get('hours_to_resolution'))
            if bias_info:
                gfs['peak'] = gfs_peak  # Обновляем peak в gfs dict
                gfs['bias_corrected'] = True
                stats['bias_corrected'] += 1

            stable, drift = self.check_gfs_stability(city, event_date_str, gfs_peak)
            
            if not stable:
                stats['skipped_stability'] += 1
                logger.info(f"   🌪️ {city}: GFS drift {drift:.1f}°C > {GFS_MAX_DRIFT}°C → нестабильно, пропускаем")
                continue
            else:
                # Обновляем историю для следующего раза
                self.update_gfs_history(city, event_date_str, gfs_peak)

            _mc = int(gfs.get("model_count") or 0)
            if MIN_MODELS_FOR_ENTRY > 1 and _mc < MIN_MODELS_FOR_ENTRY:
                stats["skipped_edge"] += 1
                logger.info(
                    f"   ⏭️ {city}: model_count {_mc} < {MIN_MODELS_FOR_ENTRY} — пропуск (качество входа)"
                )
                continue

            _sp = float(gfs.get("spread") or 0.0)
            if MAX_ENSEMBLE_SPREAD_C > 0.0 and _sp > MAX_ENSEMBLE_SPREAD_C:
                stats["skipped_edge"] += 1
                logger.info(
                    f"   ⏭️ {city}: ensemble spread {_sp:.2f}°C > {MAX_ENSEMBLE_SPREAD_C:.2f} — пропуск (качество входа)"
                )
                continue

            confidence = float(gfs.get('confidence', 0.5))
            risk_tier = gfs.get('risk_tier', self.get_city_risk_tier(city))
            size_multiplier = float(gfs.get('size_multiplier', 0.25))
            if blocked_soft:
                size_multiplier = max(0.01, size_multiplier * BLOCKED_CITIES_SOFT_STAKE_MULT)
                gfs['size_multiplier'] = size_multiplier
            tails_allowed = bool(gfs.get('tails_allowed', False))
            if confidence < CONFIDENCE_SKIP_THRESHOLD or size_multiplier <= 0:
                stats['skipped_edge'] += 1
                logger.info(f"   ⏭️ {city}: confidence {confidence:.2f} слишком низкий для входа (tier={risk_tier})")
                continue

            # Поиск аномалий
            peak_temp = round(gfs.get('peak', gfs['median']))
            peak_purchase_key = (city.lower(), peak_temp, event_date_str[:10])
            valid_temps = None

            if gfs.get('bucket_only'):
                logger.info(
                    f"   🎯 {city}: bucket-only — confidence={confidence:.2f}, tier={risk_tier}, "
                    f"size_mult={size_multiplier:.2f}"
                )
                valid_temps = []
                for m in bucket_markets:
                    parsed = self.classify_bucket_market(m.get('question', ''))
                    if not parsed:
                        continue
                    kind, th = parsed
                    item = self._try_bucket_strike(
                        city,
                        m,
                        event_date_str,
                        kind,
                        th,
                        peak_temp,
                        gfs,
                        bankroll,
                        size_multiplier,
                        confidence,
                        risk_tier,
                        stats,
                    )
                    if item:
                        valid_temps.append(item)
                if not valid_temps:
                    logger.info(f"   ⏭️ {city}: bucket-only — нет проходящих YES")
                    stats['skipped_city'] += 1
                    continue
            else:
                # 🔧 v4.1: Гибкая лестница — не all-or-nothing.
                ladder_src = list(gfs['ladder'])
                core_temps = set(gfs.get('core_temps') or ladder_src)
                tail_temps = set(gfs.get('tail_temps') or [])
                if not tails_allowed:
                    tail_temps = set()

                if len(core_temps) < 2:
                    logger.info(f"   ⏭️ {city}: Core ladder слишком маленький ({len(core_temps)} темп.) — пропускаем")
                    stats['skipped_gfs'] += 1
                    continue

                logger.info(
                    f"   🎯 {city}: confidence={confidence:.2f}, tier={risk_tier}, "
                    f"size_mult={size_multiplier:.2f}, tails={'on' if tails_allowed else 'off'}"
                )

                valid_core = []
                for temp in sorted(core_temps):
                    item = self._try_ladder_strike(
                        city,
                        markets,
                        event_date_str,
                        temp,
                        False,
                        peak_temp,
                        gfs,
                        bankroll,
                        size_multiplier,
                        confidence,
                        risk_tier,
                        stats,
                    )
                    if item:
                        valid_core.append(item)

                peak_in_valid = any(int(v["temp"]) == int(peak_temp) for v in valid_core)
                peak_held = peak_purchase_key in self.purchased_cache
                peak_satisfied = peak_in_valid or peak_held

                valid_tails = []
                if tail_temps:
                    if LADDER_PEAK_REQUIRED_FOR_TAILS and not peak_satisfied:
                        logger.info(
                            f"   ⏭️ {city}: хвосты пропущены — нет якоря по пику {peak_temp}°C "
                            f"(не проходит фильтры и нет в cache)"
                        )
                    if (not LADDER_PEAK_REQUIRED_FOR_TAILS) or peak_satisfied:
                        for temp in sorted(tail_temps):
                            item = self._try_ladder_strike(
                                city,
                                markets,
                                event_date_str,
                                temp,
                                True,
                                peak_temp,
                                gfs,
                                bankroll,
                                size_multiplier,
                                confidence,
                                risk_tier,
                                stats,
                            )
                            if item:
                                valid_tails.append(item)

                def _core_strike_satisfied(t):
                    k = (city.lower(), int(round(t)), event_date_str[:10])
                    if k in self.purchased_cache:
                        return True
                    return any(int(v["temp"]) == int(t) for v in valid_core)

                core_complete = all(_core_strike_satisfied(t) for t in core_temps)
                core_sat_n = sum(1 for t in core_temps if _core_strike_satisfied(t))

                valid_temps = None
                if core_complete:
                    valid_temps = list(valid_core) + list(valid_tails)
                    logger.info(
                        f"   ✅ {city}: полное ядро {core_sat_n}/{len(core_temps)}, tails {len(valid_tails)} — готовим покупки"
                    )
                elif peak_satisfied and (valid_core or valid_tails):
                    valid_temps = list(valid_core) + list(valid_tails)
                    logger.info(
                        f"   ✅ {city}: частичное ядро (пик якорь), новых core {len(valid_core)}, tails {len(valid_tails)} — готовим покупки"
                    )
                elif LADDER_ANCHOR_ONLY_ENABLED and len(valid_core) == 1:
                    a0 = valid_core[0]
                    d = abs(int(a0["temp"]) - int(peak_temp))
                    if (
                        d <= ANCHOR_MAX_DIST_C
                        and float(a0["edge"]) >= ANCHOR_ONLY_MIN_EDGE
                        and float(a0["market_price"]) <= ANCHOR_ONLY_MAX_PRICE
                    ):
                        valid_temps = [a0]
                        logger.info(
                            f"   ✅ {city}: ЯКОРЬ 1×YES @ {a0['temp']}°C (Δ={d}° от пика {peak_temp}°C), "
                            f"edge={float(a0['edge']):.3f}, цена=${float(a0['market_price']):.4f}"
                        )

                if valid_temps is None and ENABLE_BUCKET_MARKETS and bucket_markets:
                    fb_peak = round(gfs.get('peak', gfs['median']))
                    fb = []
                    for m in bucket_markets:
                        parsed = self.classify_bucket_market(m.get('question', ''))
                        if not parsed:
                            continue
                        kind, th = parsed
                        it = self._try_bucket_strike(
                            city,
                            m,
                            event_date_str,
                            kind,
                            th,
                            fb_peak,
                            gfs,
                            bankroll,
                            size_multiplier,
                            confidence,
                            risk_tier,
                            stats,
                        )
                        if it:
                            fb.append(it)
                    if fb:
                        valid_temps = fb
                        logger.info(f"   ✅ {city}: fallback только бакеты — {len(fb)} кандидатов")

                if valid_temps is not None and ENABLE_BUCKET_MARKETS and bucket_markets:
                    peak_mx = round(gfs.get('peak', gfs['median']))
                    existing_tokens = {str(v.get('token_id')) for v in valid_temps if v.get('token_id')}
                    for m in bucket_markets:
                        parsed = self.classify_bucket_market(m.get('question', ''))
                        if not parsed:
                            continue
                        kind, th = parsed
                        it = self._try_bucket_strike(
                            city,
                            m,
                            event_date_str,
                            kind,
                            th,
                            peak_mx,
                            gfs,
                            bankroll,
                            size_multiplier,
                            confidence,
                            risk_tier,
                            stats,
                        )
                        if it and str(it.get('token_id')) not in existing_tokens:
                            valid_temps.append(it)
                            existing_tokens.add(str(it.get('token_id')))

            if valid_temps is None:
                no_signal = self.maybe_build_no_signal(
                    city,
                    markets,
                    event_date_str,
                    peak_temp,
                    gfs,
                    confidence,
                    risk_tier,
                    size_multiplier,
                    active_positions,
                    bankroll,
                )
                if no_signal:
                    signals.append(no_signal)
                    stats['signals'] += 1
                    logger.info(
                        f"   🛡️ NO-сигнал: {city} {no_signal['temp']}°C — "
                        f"Edge: +{no_signal['edge']*100:.1f}% @ {no_signal['price']*100:.1f}¢"
                    )
                    self.write_telemetry("entry_signal", {
                        'city': city,
                        'temp': no_signal['temp'],
                        'date': no_signal['date'][:10],
                        'edge': no_signal['edge'],
                        'model_prob': no_signal['model_prob'],
                        'exec_price': no_signal['price'],
                        'stake': no_signal['stake'],
                        'is_tail': False,
                        'is_no_hedge': True,
                        'confidence': no_signal.get('confidence'),
                        'risk_tier': no_signal.get('risk_tier'),
                        'size_multiplier': no_signal.get('size_multiplier'),
                        'token_id': no_signal.get('token_id'),
                        'side': 'NO',
                    })
                    continue
                logger.info(
                    f"   ❌ {city}: нет допустимого YES (ядро {core_sat_n}/{len(core_temps)}, пик якорь={peak_satisfied}) — пропуск"
                )
                stats['skipped_city'] += 1
                continue

            def _strike_sort_key(x):
                ref = x['threshold_c'] if x.get('is_bucket') else int(x['temp'])
                return (abs(ref - peak_temp), x['temp'])

            valid_temps = sorted(valid_temps, key=_strike_sort_key)
            if not valid_temps:
                logger.info(f"   ⏭️ {city}: нет новых YES для покупки (всё в cache / пустой набор)")
                continue

            # Шаг 4: Формируем сигналы по городу атомарно.
            city_signals = []
            city_purchase_keys = []
            city_allowed = True
            for vt in valid_temps:
                temp = vt['temp']
                market_price = vt['market_price']
                model_prob = vt['model_prob']
                edge = vt['edge']
                sigma = vt['sigma']
                target = vt['target']
                is_tail = vt.get('is_tail', False)
                token_id = vt.get('token_id')
                ref_t = int(vt['threshold_c']) if vt.get('is_bucket') else int(temp)

                dist = abs(ref_t - peak_temp)
                stake_sigma = 1.0
                weight = math.exp(-0.5 * (dist / stake_sigma) ** 2)

                base_stake = bankroll * MAX_POSITION_PCT * vt.get('size_multiplier', size_multiplier)
                stake = base_stake * weight
                stake = max(MIN_BET_USD, min(stake, MAX_BET_USD))

                # Проверка лимитов
                purchase_key = (city.lower(), int(round(temp)), event_date_str[:10])
                allowed, reason = self.check_limits(
                    city, event_date_str, stake, active_positions, target_temp=ref_t
                )

                if not allowed:
                    lbl = vt.get('signal_label', f"{temp}°C")
                    logger.info(f"   ❌ {city} {lbl}: Лимит нарушен ({reason}) — отменяем город")
                    city_allowed = False
                    break

                city_purchase_keys.append(purchase_key)
                city_signals.append({
                    'market': target,
                    'city': city,
                    'temp': temp,
                    'price': market_price,
                    'edge': edge,
                    'model_prob': model_prob,
                    'weight': weight,
                    'date': event_date_str,
                    'stake': min(stake, MAX_BET_USD),
                    'aggressive': False,
                    'is_tail': is_tail,
                    'is_bucket': vt.get('is_bucket', False),
                    'signal_label': vt.get('signal_label'),
                    'confidence': vt.get('confidence', confidence),
                    'risk_tier': vt.get('risk_tier', risk_tier),
                    'size_multiplier': vt.get('size_multiplier', size_multiplier),
                    'token_id': token_id,
                    'purchase_key': purchase_key,
                    'reprice_count': self.pending_reprice_budget.get(purchase_key, 0),
                    'outcome_name': 'Yes',
                    'side_label': 'YES',
                })

            if not city_allowed:
                continue

            for purchase_key, signal in zip(city_purchase_keys, city_signals):
                signals.append(signal)
                stats['signals'] += 1
                tail_tag = " [TAIL]" if signal.get('is_tail') else ""
                bkt = " [BUCKET]" if signal.get('is_bucket') else ""
                lab = signal.get('signal_label') or format_purchase_temp_label(signal['temp'])
                logger.info(f"   ✅ Сигнал: {city} {lab}{bkt}{tail_tag} — Edge: +{signal['edge']*100:.1f}%")
                self.write_telemetry("entry_signal", {
                    'city': city,
                    'temp': signal['temp'],
                    'date': signal['date'][:10],
                    'edge': signal['edge'],
                    'model_prob': signal['model_prob'],
                    'exec_price': signal['price'],
                    'stake': signal['stake'],
                    'is_tail': signal.get('is_tail', False),
                    'is_bucket': signal.get('is_bucket', False),
                    'confidence': signal.get('confidence'),
                    'risk_tier': signal.get('risk_tier'),
                    'size_multiplier': signal.get('size_multiplier'),
                    'token_id': signal.get('token_id'),
                    'side': signal.get('side_label', 'YES'),
                })
        
        # Дальше — стандартная логика: token_id для каждого сигнала

        # 3. Вывод и Торговля
        signals = self.prioritize_signals(signals)
        signals = self.cap_signals_by_city_date(signals)
        
        # Печатаем отчет сканирования
        print(f"\n{C.BOLD}🔍 Отчет сканирования:{C.ENDC}")
        print(f"   Просмотрено событий: {stats['scanned']}")
        print(f"   ⏭️ Пропущено (не тот город): {stats['skipped_city']}")
        print(f"   ⏭️ Пропущено (время):    {stats['skipped_time']}")
        print(f"   ⏭️ Пропущено (ликвидность): {stats['skipped_liq']}")
        print(f"   ⏭️ Пропущено (GFS нет): {stats['skipped_gfs']}")
        print(f"   🌪️ Пропущено (drift):    {stats['skipped_stability']}")
        print(f"   🚫 Пропущено (city stop): {stats['skipped_city_stoploss']}")
        print(f"   ⏭️ Пропущено (малый Edge): {stats['skipped_edge']}")
        print(f"   ⏭️ Пропущено (дорого):   {stats['skipped_price']}")
        # stats['signals'] — кандидаты до prioritize/cap; len(signals) — реально к исполнению
        # (cap_signals_by_city_date может отсечь лишнее при лимите страйков на город/день).
        print(f"   ✅ Найдено сигналов: {len(signals)}")
        if stats['signals'] != len(signals):
            print(
                f"   ℹ️  Кандидатов до лимита по городу/дню: {stats['signals']} "
                f"(отсечено {stats['signals'] - len(signals)} — см. ⏭️ «Лимит страйков на город/день» в логе)"
            )
        print(f"\n   🌡️ GFS Source:")
        print(f"      ✅ Real GFS: {stats['gfs_real']}")
        print(f"      ⚠️  Fallback: {stats['gfs_fallback']}")
        print(f"      🔧 Bias corrected: {stats['bias_corrected']}")
        
        if stats['scanned'] > 0 and stats['skipped_time'] == stats['scanned']:
            print(f"\n   {C.WARNING}⚠️ Все найденные рынки уже прошли экспирацию!")
            print(f"   Polymarket ещё не создал новые температурные рынки.{C.ENDC}")

        # Показываем local cache покупок
        if self.purchased_cache:
            print(f"\n   🛡️ Local cache покупок: {len(self.purchased_cache)}")
            for key in sorted(self.purchased_cache):
                print(f"      • {key[0].upper()} {format_purchase_temp_label(key[1])} {key[2]}")
        
        print(f"\n{C.BOLD}📊 Сигналы:{C.ENDC}")
        
        total_spend = 0
        yes_count = 0
        no_count = 0

        for s in signals:
            # 🛡️ ЛИМИТ: не больше MAX_SIGNALS_PER_SCAN за скан
            if yes_count + no_count >= MAX_SIGNALS_PER_SCAN:
                logger.info(f"   ⏭️ Лимит скана ({MAX_SIGNALS_PER_SCAN}) достигнут, пропускаем остальные")
                break
                
            # 🛡️ ЛИМИТ: не больше session_budget
            if total_spend >= self.session_budget:
                logger.info(f"   ⏭️ Бюджет сессии (${self.session_budget:.2f}) исчерпан, пропускаем")
                break
            
            agg_tag = " 🔥AGGR" if s['aggressive'] else ""
            tail_tag = " 🌙TAIL" if s.get('is_tail') else ""
            no_tag = " 🛡️NO" if s.get('is_no_hedge', False) else " ✅YES"
            if s.get('is_no_hedge', False):
                no_count += 1
            else:
                yes_count += 1
            model_p = s.get('model_prob', 0) * 100
            _disp = s.get('signal_label') or format_purchase_temp_label(s['temp'])
            print(f"   {no_tag} {s['city'].upper()} | {_disp} | Edge: +{s['edge']*100:.1f}% (Prob: {model_p:.1f}%){agg_tag}{tail_tag}")
            print(f"      Цена входа: {s['price']*100:.1f}¢ -> Ставка: ${s['stake']:.2f}")

            # Те же preflight-проверки должны работать и в DRY RUN, иначе тесты не отражают live.
            purchase_key = s.get('purchase_key') or (s['city'].lower(), int(s['temp']), s['date'][:10])
            token_id = s.get('token_id')

            sig_temp = int(round(s['temp']))
            if (
                ENFORCE_MAX_EXACT_TEMPS_PER_CITY_DATE
                and MAX_EXACT_TEMPS_PER_CITY_DATE > 0
                and not s.get('is_bucket', False)
                and not s.get('is_no_hedge', False)
            ):
                ex_set = self._exact_temps_in_cache(s['city'], s['date'])
                if sig_temp not in ex_set and len(ex_set) >= MAX_EXACT_TEMPS_PER_CITY_DATE:
                    logger.info(
                        f"   ⏭️ SKIP (лимит exact на город/день): {s['city'].upper()} "
                        f"{format_purchase_temp_label(sig_temp)} — в cache уже {len(ex_set)} exact"
                    )
                    continue

            if purchase_key in self.purchased_cache:
                logger.warning(
                    f"   ⛔ SKIP (в памяти): {s['city']} {format_purchase_temp_label(s['temp'])} — уже куплено!"
                )
                continue

            if token_id and str(token_id) in self.purchased_token_cache:
                logger.warning(f"   ⛔ SKIP (token-cache): {s['city']} {format_purchase_temp_label(s['temp'])} — token уже куплен!")
                continue

            if purchase_key in self.pending_order_cache:
                logger.warning(f"   ⛔ SKIP (pending): {s['city']} {format_purchase_temp_label(s['temp'])} — уже есть открытый ордер!")
                continue

            if purchase_key in self.pending_reprice_blocked:
                logger.warning(f"   ⛔ SKIP (reprice limit): {s['city']} {format_purchase_temp_label(s['temp'])} — лимит reprices исчерпан в этом цикле")
                continue

            if token_id and str(token_id) in self.pending_token_cache:
                logger.warning(f"   ⛔ SKIP (pending token): {s['city']} {format_purchase_temp_label(s['temp'])} — open order уже размещён!")
                continue

            disk_duplicate_found = False
            if os.path.exists(self.purchases_file):
                try:
                    with open(self.purchases_file, 'r') as f:
                        disk_data = json.load(f)
                    for item in disk_data:
                        disk_key = (item.get('city', '').lower(), int(item.get('temp', 0)), item.get('date', '')[:10])
                        disk_token = str(item.get('token_id', '') or '')
                        if disk_key == purchase_key or (token_id and disk_token == str(token_id)):
                            logger.warning(f"   ⛔ SKIP (на диске): {s['city']} {format_purchase_temp_label(s['temp'])} — уже есть в purchases.json!")
                            disk_duplicate_found = True
                            break
                except Exception:
                    pass
            if disk_duplicate_found:
                continue

            api_duplicate_found = False
            try:
                api_positions = self.get_balance_and_positions()[1]
                for pos in api_positions:
                    pos_token = str(pos.get('asset') or pos.get('asset_id') or pos.get('token_id') or '')
                    if token_id and pos_token and pos_token == str(token_id):
                        pos_size = pos.get('size', 0)
                        if pos_size > 0:
                            logger.warning(f"   ⛔ SKIP (API token): {s['city']} {format_purchase_temp_label(s['temp'])} — token уже есть на кошельке (size={pos_size})!")
                            api_duplicate_found = True
                            break
                    pos_title = pos.get('title', '').lower()
                    title_temps = _title_strike_temps_c(pos_title)
                    if s['city'].lower() in pos_title and int(s['temp']) in title_temps:
                        pos_size = pos.get('size', 0)
                        if pos_size > 0:
                            # Иначе Munich 12°C за 6 Apr блокирует сигнал на 8 Apr — разные рынки, один city/temp в title.
                            pos_end = _position_end_date_iso(pos)
                            sig_date = (s.get('date') or '')[:10]
                            if pos_end and sig_date and pos_end == sig_date:
                                logger.warning(f"   ⛔ SKIP (API): {s['city']} {format_purchase_temp_label(s['temp'])} — уже есть на кошельке (size={pos_size})!")
                                api_duplicate_found = True
                                break
            except Exception:
                pass
            if api_duplicate_found:
                continue

            buy_allowed, buy_count = self.check_buy_limit(s['city'], s['temp'], s['date'])
            if not buy_allowed:
                logger.warning(f"   ⛔ SKIP (лимит exact market): {s['city'].upper()} {format_purchase_temp_label(s['temp'])} {s['date'][:10]} — {buy_count}/{self.MAX_BUYS_PER_MARKET}, рынок исчерпан!")
                continue
            if not token_id:
                logger.warning(f"   ⚠️ Нет token_id, пропускаем покупку")
                continue

            planned_contracts = max(1, round(s['stake'] / max(s['price'], 0.01)))
            live_quote = self.get_cached_quote(
                token_id,
                contracts=planned_contracts,
                fallback_price=s['price'],
                market=s.get('market'),
                outcome_name=s.get('outcome_name', 'Yes'),
            )
            min_edge_required = NO_MIN_EDGE if s.get('is_no_hedge', False) else (TAIL_MIN_EDGE if s.get('is_tail') else MIN_EDGE)
            max_price_allowed = float(
                s.get('max_price_allowed')
                or (
                    self.get_no_price_limit(s.get('risk_tier'), s.get('confidence'))
                    if s.get('is_no_hedge', False)
                    else self.get_entry_price_limit(
                        s.get('risk_tier'),
                        s.get('confidence'),
                        is_tail=s.get('is_tail', False),
                    )
                )
            )
            live_price = self.get_execution_price(s, live_quote, max_price_allowed, min_edge_required)
            live_edge = s.get('model_prob', 0) - (live_price + POLYMARKET_COMMISSION)
            pass_abs_live = (
                live_price < EDGE_ABSOLUTE_PRICE_THRESHOLD
                and live_edge >= EDGE_ABSOLUTE_MIN
            )

            if live_price >= max_price_allowed:
                logger.info(f"   ⏭️ BUY skip: стакан сдвинулся, цена {live_price:.4f} > лимита {max_price_allowed:.4f}")
                self.write_telemetry("entry_skip", {
                    'city': s['city'],
                    'temp': s['temp'],
                    'date': s['date'][:10],
                    'reason': 'price_moved',
                    'live_price': live_price,
                    'max_price_allowed': max_price_allowed,
                })
                continue

            if not pass_abs_live and live_edge < min_edge_required:
                logger.info(f"   ⏭️ BUY skip: live edge {live_edge:.3f} < {min_edge_required:.3f}")
                self.write_telemetry("entry_skip", {
                    'city': s['city'],
                    'temp': s['temp'],
                    'date': s['date'][:10],
                    'reason': 'live_edge_below_threshold',
                    'live_edge': live_edge,
                    'min_edge_required': min_edge_required,
                    'live_price': live_price,
                })
                continue

            if not DRY_RUN and self.client is None:
                logger.error(f"   ❌ ClobClient не инициализирован, пропускаем покупку")
                continue

            market_lock_path = self.acquire_market_lock(purchase_key, token_id=token_id)
            if not market_lock_path:
                logger.warning(f"   ⛔ SKIP (market lock): {s['city']} {format_purchase_temp_label(s['temp'])} — другой процесс уже резервирует этот рынок")
                continue

            try:
                result = place_bet(
                    token_id,
                    live_price,
                    s['stake'],
                    self.client,
                    side_label=s.get('side_label', 'YES'),
                )

                if result['status'] == 'FILLED':
                    logger.info(f"   ✅ Order Placed: {result['contracts']} contracts, total cost ${result['cost']:.2f}")
                    total_spend += result['cost']
                    self.purchased_cache.add(purchase_key)
                    self.purchased_token_cache.add(str(token_id))
                    self.clear_pending_order(purchase_key=purchase_key, token_id=token_id, order_id=result.get('order_id'))
                    self.save_purchase(s['city'], s['temp'], s['date'], token_id=token_id)
                    logger.info(f"   🛡️ Добавлено в cache + сохранено: {purchase_key}")
                    self.increment_buy_counter(s['city'], s['temp'], s['date'])
                    self.write_telemetry("entry_filled", {
                        'city': s['city'],
                        'temp': s['temp'],
                        'date': s['date'][:10],
                        'exec_price': live_price,
                        'edge': live_edge,
                        'model_prob': s['model_prob'],
                        'contracts': result['contracts'],
                        'cost': result['cost'],
                        'token_id': token_id,
                        'side': s.get('side_label', 'YES'),
                    })
                    mode_tag = "🔥AGGR" if s['aggressive'] else "📊"
                    side_tag = s.get('side_label', 'YES')
                    msg = (
                        f"<b>✅ WEATHER BOT v3.9 — BUY EXECUTED [LIVE]</b>\n"
                        f"\n"
                        f"{mode_tag} <b>{s['city'].upper()}</b> | {format_purchase_temp_label(s['temp'])} | {side_tag}\n"
                        f"📅 Дата: {s['date'][:10]}\n"
                        f"💰 Цена: {live_price*100:.1f}¢\n"
                        f"📈 Edge: +{live_edge*100:.1f}% (Prob: {s['model_prob']*100:.1f}%)\n"
                        f"📦 Контрактов: {result['contracts']}\n"
                        f"💵 Расход: ${result['cost']:.2f}\n"
                        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
                    )
                    send_telegram(msg)
                elif result['status'] == 'POSTED':
                    logger.info(
                        f"   🟡 Order posted to book: {result['contracts']} contracts, "
                        f"status={result.get('exchange_status', 'live')} cost=${result['cost']:.2f}"
                    )
                    total_spend += result['cost']
                    self.save_pending_order(
                        s['city'],
                        s['temp'],
                        s['date'],
                        token_id=token_id,
                        order_id=result.get('order_id'),
                        side=s.get('side_label', 'YES'),
                        price=live_price,
                        contracts=result['contracts'],
                        exchange_status=result.get('exchange_status', 'posted'),
                        reprice_count=s.get('reprice_count', 0),
                        aggressive=s.get('aggressive', False),
                        model_prob=s.get('model_prob'),
                        edge=live_edge,
                        confidence=s.get('confidence'),
                        risk_tier=s.get('risk_tier'),
                        is_tail=s.get('is_tail', False),
                        is_no_hedge=s.get('is_no_hedge', False),
                        signal_score=s.get('priority_score'),
                    )
                    self.write_telemetry("entry_posted", {
                        'city': s['city'],
                        'temp': s['temp'],
                        'date': s['date'][:10],
                        'exec_price': live_price,
                        'edge': live_edge,
                        'model_prob': s['model_prob'],
                        'contracts': result['contracts'],
                        'cost': result['cost'],
                        'token_id': token_id,
                        'side': s.get('side_label', 'YES'),
                        'order_id': result.get('order_id'),
                        'exchange_status': result.get('exchange_status'),
                    })
                    mode_tag = "🔥AGGR" if s['aggressive'] else "📊"
                    side_tag = s.get('side_label', 'YES')
                    msg = (
                        f"<b>🟡 WEATHER BOT v3.9 — ORDER POSTED [LIVE]</b>\n"
                        f"\n"
                        f"{mode_tag} <b>{s['city'].upper()}</b> | {format_purchase_temp_label(s['temp'])} | {side_tag}\n"
                        f"📅 Дата: {s['date'][:10]}\n"
                        f"💰 Лимит: {live_price*100:.1f}¢\n"
                        f"📈 Edge: +{live_edge*100:.1f}% (Prob: {s['model_prob']*100:.1f}%)\n"
                        f"📦 Ордер: {result['contracts']} контрактов\n"
                        f"💵 Потенциальный расход: ${result['cost']:.2f}\n"
                        f"📌 Статус биржи: {result.get('exchange_status', 'live')}\n"
                        f"⏰ {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
                    )
                    send_telegram(msg)
                elif result['status'] == 'DRY_RUN':
                    logger.info(f"   🧪 DRY RUN: {result['contracts']} contracts @ ${result['cost']:.2f}")
                    total_spend += result['cost']
                    self.purchased_cache.add(purchase_key)
                    self.purchased_token_cache.add(str(token_id))
                    self.write_telemetry("entry_simulated", {
                        'city': s['city'],
                        'temp': s['temp'],
                        'date': s['date'][:10],
                        'exec_price': live_price,
                        'edge': live_edge,
                        'model_prob': s['model_prob'],
                        'contracts': result['contracts'],
                        'cost': result['cost'],
                        'token_id': token_id,
                        'side': s.get('side_label', 'YES'),
                    })
                elif result['status'] == 'SKIP':
                    logger.info(f"   ⏭️ Skip: {result['reason']}")
                elif result['status'] == 'ERROR':
                    logger.error(f"   ❌ Error: {result['reason']}")
            finally:
                self.release_market_lock(market_lock_path)

        wallet_end = get_wallet_balance_api(FUNDER_ADDRESS)
        wallet_estimated = False
        if wallet_end is None:
            wallet_end = self.get_usdc_balance_rpc(FUNDER_ADDRESS)
        if wallet_end is None:
            wallet_end = max(0.0, float(getattr(self, "_risk_bankroll", BANKROLL) or BANKROLL) - total_spend)
            wallet_estimated = True

        print(f"\n{C.BOLD}{'═'*60}{C.ENDC}")
        print(f"   Найдено сигналов: {len(signals)} (YES: {yes_count}, NO: {no_count})")
        print(f"   Потрачено в этом скане: ${total_spend:.2f}")
        _pct = SESSION_BUDGET_PCT * 100.0
        _scan_rem = self.session_budget - total_spend
        print(
            f"   Лимит на скан ({_pct:.0f}% USDC на старт): ${self.session_budget:.2f}  "
            f"→  осталось от лимита: ${_scan_rem:.2f}"
        )
        _bal_note = " (оценка: старт − расход)" if wallet_estimated else ""
        print(f"   Баланс USDC (кошелёк){_bal_note}: ${wallet_end:.2f}")
        print(f"{'═'*60}")
        hot_expiry = bool(
            AI_WEATHER
            and HOT_HOURS_BEFORE_EXPIRY > 0
            and self._scan_min_hours_to_expiry is not None
            and self._scan_min_hours_to_expiry <= HOT_HOURS_BEFORE_EXPIRY
        )
        self.last_scan_summary = {
            'signals': len(signals),
            'total_spend': total_spend,
            'max_edge': max((s.get('edge', 0) for s in signals), default=0),
            'exit_actions': exit_summary.get('actions', 0),
            'exit_hot': exit_summary.get('hot', False),
            'exit_fast': exit_summary.get('fast', False),
            'hot_expiry': hot_expiry,
        }
        if hot_expiry and self._scan_min_hours_to_expiry is not None:
            logger.info(
                f"   ⚡ Hot expiry: min_hours_to_expiry={self._scan_min_hours_to_expiry:.2f} "
                f"≤ {HOT_HOURS_BEFORE_EXPIRY}h → polling может уйти на {HOT_SCAN_INTERVAL_SEC}s"
            )

    def get_next_scan_interval(self):
        base_interval, status, _ = get_gfs_scan_interval()
        next_interval = base_interval
        summary = self.last_scan_summary or {}

        if summary.get('hot_expiry') or summary.get('exit_hot'):
            next_interval = min(next_interval, HOT_SCAN_INTERVAL_SEC)
        elif summary.get('exit_fast') or summary.get('exit_actions', 0) > 0:
            next_interval = min(next_interval, FAST_SCAN_INTERVAL_SEC)
        elif summary.get('signals', 0) > 0 or summary.get('max_edge', 0) >= max(MIN_EDGE * 2, 0.08):
            next_interval = min(next_interval, FAST_SCAN_INTERVAL_SEC)

        if next_interval < base_interval:
            logger.info(
                f"   ⚡ Следующий интервал: {next_interval}s (база GFS {base_interval}s); "
                f"hot_expiry={summary.get('hot_expiry')} exit_hot={summary.get('exit_hot')} "
                f"exit_fast={summary.get('exit_fast')} signals={summary.get('signals', 0)}"
            )

        mode = "stream-ready" if STREAMING_READY else "polling"
        return max(5, int(next_interval)), f"{status} | {mode}"


def get_gfs_scan_interval():
    """
    🌡️ Рассчитывает интервал сканирования на основе расписания GFS
    
    Возвращает:
    - 10 мин: если GFS данные свежие (0-6ч после обновления)
    - 30 мин: если данные нормальные (6-12ч)
    - 60 мин: если данные старые (>12ч)
    """
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    
    # Находим ближайший GFS run
    hours_since_gfs = None
    for gfs_hour in GFS_RUNS_UTC:
        # Сколько часов прошло с этого GFS run
        if current_hour >= gfs_hour:
            hours_passed = current_hour - gfs_hour
        else:
            hours_passed = 24 - (gfs_hour - current_hour)
        
        # Учитываем загрузку данных
        data_available = hours_passed - GFS_DATA_DELAY_HOURS
        
        if hours_since_gfs is None or (0 <= data_available < hours_since_gfs):
            hours_since_gfs = data_available
    
    # Определяем интервал
    if hours_since_gfs is not None and 0 <= hours_since_gfs <= GFS_ACTIVE_WINDOW_HOURS:
        interval = SCAN_INTERVAL_GFS_ACTIVE
        status = "🔴 ACTIVE (GFS свежие)"
    elif hours_since_gfs is not None and GFS_ACTIVE_WINDOW_HOURS < hours_since_gfs <= 12:
        interval = SCAN_INTERVAL_GFS_NORMAL
        status = "🟡 NORMAL"
    else:
        interval = SCAN_INTERVAL_GFS_IDLE
        status = "🟢 IDLE (GFS старые)"
    
    return interval, status, hours_since_gfs

if __name__ == "__main__":
    try:
        bot = WeatherBotV3()
    except RuntimeError as e:
        logger.error(f"   ❌ Startup blocked: {e}")
        raise SystemExit(1)

    if AUTO_SCAN:
        # 🔄 ЦИКЛИЧНЫЙ РЕЖИМ С GFS-ПРИВЯЗКОЙ
        print(f"\n🔄 АВТОСКАН: GFS-привязанное расписание")
        print(f"   GFS runs: 00:00, 06:00, 12:00, 18:00 UTC")
        print(f"   Active (свежие данные): каждые {SCAN_INTERVAL_GFS_ACTIVE//60} мин")
        print(f"   Normal: каждые {SCAN_INTERVAL_GFS_NORMAL//60} мин")
        print(f"   Idle (старые данные): каждые {SCAN_INTERVAL_GFS_IDLE//60} мин")
        print(f"   Ctrl+C для остановки\n")

        scan_count = 0
        try:
            while True:
                scan_count += 1
                
                # Рассчитываем интервал на основе GFS
                interval, status, hours_since_gfs = get_gfs_scan_interval()
                
                print(f"\n{'='*60}")
                print(f"🔄 Скан #{scan_count} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
                print(f"   {status} | Часов с GFS: {hours_since_gfs:.0f} | Интервал: {interval//60} мин")
                print(f"{'='*60}")

                bot.run()
                interval, status = bot.get_next_scan_interval()

                print(f"\n⏳ Следующий скан через {interval//60} мин ({status})...")
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n\n⏹️ Остановлено пользователем")
            print(f"   Всего сканов: {scan_count}")
            try:
                bot.orderbook_ws.stop()
            except Exception:
                pass
            try:
                send_telegram(f"⏹️ <b>WEATHER BOT</b> остановлен\nВсего сканов: {scan_count}")
            except Exception:
                pass
    else:
        # ОДИНОЧНЫЙ РЕЖИМ
        bot.run()
