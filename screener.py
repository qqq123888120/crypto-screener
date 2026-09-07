#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安USDT永续合约 - EMA20/50/200 回踩确认 形态筛选器
--------------------------------------------------
逻辑（模仿你截图里 IOST 的走势）:
1. EMA20 / EMA50 / EMA200 三线粘合(说明前期横盘打底，均线纠缠)
2. 最近K线价格从上方或均线区回踩到 EMA20/50 附近，又收回到均线上方(确认支撑)
3. 24小时涨幅还不大(排除已经暴涨过的币，只找"还没涨起来"的)
4. 成交量较前期温和放大(MA5 > MA10，说明资金在悄悄进场)

不构成投资建议，仅做技术形态筛选，请自行判断风险。
"""

import time
import requests
import pandas as pd
import os

FUTURES_BASE = "https://fapi.binance.com"

# ---------- 可调参数 ----------
INTERVAL = "4h"              # 和你截图一致的4小时周期
KLINE_LIMIT = 300            # 至少要 > 200根才能算出稳定的EMA200
EMA_CLUSTER_PCT = 0.035      # 三条EMA之间的最大差距(3.5%以内算"粘合")
MAX_24H_CHANGE = 15.0        # 24h涨幅超过这个值就跳过(说明已经涨过了)
MIN_24H_QUOTE_VOLUME = 5_000_000  # 24h成交额(USDT)最低门槛，过滤掉流动性太差的币
TOUCH_TOLERANCE = 0.015      # 判断"是否碰到EMA"的容差(1.5%)
SLEEP_BETWEEN_REQUESTS = 0.25  # 避免触发币安接口限速

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_usdt_perpetual_symbols():
    """获取所有 USDT 本位永续合约交易对"""
    url = f"{FUTURES_BASE}/fapi/v1/exchangeInfo"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    symbols = []
    for s in data["symbols"]:
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ):
            symbols.append(s["symbol"])
    return symbols


def get_24h_ticker(symbol):
    url = f"{FUTURES_BASE}/fapi/v1/ticker/24hr"
    resp = requests.get(url, params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_klines(symbol, interval=INTERVAL, limit=KLINE_LIMIT):
    url = f"{FUTURES_BASE}/fapi/v1/klines"
    resp = requests.get(
        url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10
    )
    resp.raise_for_status()
    raw = resp.json()
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def compute_emas(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    return df


def check_pattern(df):
    """
    判断是否符合"EMA粘合 + 回踩确认"形态
    返回 (是否符合, 备注信息)
    """
    if len(df) < 210:
        return False, "数据不足"

    last = df.iloc[-1]
    prev = df.iloc[-6:-1]  # 最近5根(不含当前) 用来判断是否发生过回踩

    ema20, ema50, ema200 = last["ema20"], last["ema50"], last["ema200"]
    price = last["close"]

    # 1. 三条EMA是否粘合
    ema_values = [ema20, ema50, ema200]
    spread = (max(ema_values) - min(ema_values)) / min(ema_values)
    if spread > EMA_CLUSTER_PCT:
        return False, f"EMA未粘合(离散度{spread:.2%})"

    # 2. 当前价格需站上EMA20(说明回踩后已收复)
    if price < ema20:
        return False, "价格仍在EMA20下方"

    # 3. 最近几根K线是否有回踩到EMA20/50附近再拉回
    touched = False
    for _, row in prev.iterrows():
        low = row["low"]
        for ema_val in [row["ema20"], row["ema50"]]:
            if ema_val > 0 and abs(low - ema_val) / ema_val <= TOUCH_TOLERANCE:
                touched = True
                break
        if touched:
            break
    if not touched:
        return False, "近期未出现明确回踩动作"

    # 4. 成交量是否温和放大 (MA5 > MA10)
    vol_ma5 = df["volume"].tail(5).mean()
    vol_ma10 = df["volume"].tail(10).mean()
    if vol_ma5 <= vol_ma10:
        return False, "量能未放大"

    return True, f"价格{price:.6g} / EMA离散度{spread:.2%} / 量能MA5>{'MA10'}"


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[警告] 未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，仅打印结果:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    if resp.status_code != 200:
        print("Telegram发送失败:", resp.text)


def main():
    symbols = get_usdt_perpetual_symbols()
    hits = []

    for symbol in symbols:
        try:
            ticker = get_24h_ticker(symbol)
            change_pct = float(ticker["priceChangePercent"])
            quote_volume = float(ticker["quoteVolume"])

            # 过滤: 已经涨太多的 / 流动性太差的
            if change_pct > MAX_24H_CHANGE:
                continue
            if quote_volume < MIN_24H_QUOTE_VOLUME:
                continue

            df = get_klines(symbol)
            df = compute_emas(df)
            matched, note = check_pattern(df)
            if matched:
                hits.append((symbol, change_pct, note))

        except Exception as e:
            print(f"跳过 {symbol}: {e}")
        finally:
            time.sleep(SLEEP_BETWEEN_REQUESTS)

    if hits:
        lines = ["📊 <b>EMA回踩确认 筛选结果</b>", f"（{INTERVAL}周期，共{len(hits)}个）", ""]
        for symbol, change_pct, note in hits:
            lines.append(f"• <b>{symbol}</b>  24h:{change_pct:+.2f}%  {note}")
        message = "\n".join(lines)
    else:
        message = "本次筛选未发现符合条件的合约标的。"

    send_telegram(message)
    print(message)


if __name__ == "__main__":
    main()
