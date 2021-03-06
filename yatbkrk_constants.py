#!/usr/bin/env python3

TRADE_CHECK_WAIT_TIME = {'15m': 90, '4h': 300, '1d': 300}
TRADE_EXPIRY_TIME = {'15m': 2700.0, '4h': 43200.0, '1d': 43200.0}
KRK_INTERVALS = {'15m': 15, '4h': 240, '1d': 1440}
PRIORITIES = {'15m': 4, '4h': 3, '1d': 1}
SELL_PERCENTAGE = {'15m': 1.0007, '4h': 1.003, '1d': 1.005}
NONCE = [0.1, 0.2, 0.3, 0.4, 0.5]
STABLE_COINS = ['USDC', 'USDT', 'PAX', 'ONG', 'TUSD', 'BUSD', 'ONT', 'GUSD', 'DGX', 'DAI', 'UST']
STABLE_FIAT_COINS = ["USDT", "CAD", "GBP", "USDC", "EUR", "CHZ", "JPY", "PAX", "DAI", "CHF"]
PRICE_REDUCTOR = [0.9999, 0.99985, 0.9998, 0.99975, 0.9997]
FEES = {'0': 0.0016, '49999': 0.0014, '99999': 0.0012, '249999': 0.001, '499999': 0.0008, '999999': 0.0006,
        '2499999': 0.0004, '4999999': 0.0002, '9999999': 0.0}
