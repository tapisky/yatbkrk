#!/usr/bin/env python3
import asyncio
import time
import logging
import yaml
import sys
import traceback
import json
import cryptocom.exchange as cro
import krakenex
import pickle
import os.path
import requests
import datetime

from datetime import datetime as datetime_helper
from datetime import timedelta
from requests import Request, Session
from requests.exceptions import ConnectionError, Timeout, TooManyRedirects
from os.path import exists
from cryptocom.exchange.structs import Pair
from cryptocom.exchange.structs import PrivateTrade
from binance.client import Client as Client
from binance.exceptions import *
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Wrapper for Binance API (helps getting through the recvWindow issue)
class Binance:
    def __init__(self, public_key = '', secret_key = '', sync = False):
        self.time_offset = 0
        self.b = Client(public_key, secret_key)

        if sync:
            self.time_offset = self._get_time_offset()

    def _get_time_offset(self):
        res = self.b.get_server_time()
        return res['serverTime'] - int(time.time() * 1000)

    def synced(self, fn_name, **args):
        args['timestamp'] = int(time.time() - self.time_offset)

async def main(config):
    iteration = 0

    stable_coins = ['USDCUSDT', 'PAXUSDT', 'ONGUSDT', 'TUSDUSDT', 'BUSDUSDT', 'ONTUSDT', 'GUSDUSDT', 'DGXUSDT', 'DAIUSDT', 'USTUSDT']

    # Binance API setup
    binance = Binance(public_key=config['bnb_api_key'], secret_key=config['bnb_api_secret'], sync=True)
    bnb_exchange = binance.b

    # Setup Binance availble USDT pairs
    tickers = bnb_exchange.get_all_tickers()
    usdt_tickers = [item for item in tickers if 'USDT' in item['symbol']]

    # Remove Stable coins
    usdt_tickers = [item for item in usdt_tickers if item['symbol'] not in stable_coins]
    usdt_tickers = list(map(lambda x: x['symbol'], usdt_tickers))

    balance = get_balance(config['sheet_id'])
    if config['sim_mode_on']:
        logger.info(f"Start Simulation balance = {balance}")
    else:
        logger.info(f"Start Balance = {balance}")

    # Initialize last dust transfer (we can convert dust in Binance every 6 hours => 21600 seconds)
    next_dust_transfer = time.time() + 21600

    # TAAPI API setup
    # TBD
    trades = []
    ongoingTradePairs = []

    #  Initialize opps field in googlesheet
    update_google_sheet_opps(config['sheet_id'], "")

    if config['telegram_notifications_on']:
        if config['sim_mode_on']:
            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB SIM> Starting simulation...")
        else:
            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> Starting the real thing...")

    while True:
        try:
            iteration += 1
            logger.info(f'------------ Iteration {iteration} ------------')
            logger.info("Trades ==========>")
            logger.info(trades)

            # Update google sheet status field
            dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
            statusMessage = f"{dateStamp} -- Iteration {iteration}"
            if trades:
                ongoing_trades = list(trade['pair'] + " " + trade['interval'] for trade in trades)
                statusMessage += f" (Ongoing trades: {ongoing_trades})"
            for _ in range(5):
                try:
                    update_google_sheet_status(config['sheet_id'], statusMessage)
                    break
                except:
                    logger.info(traceback.format_exc())
                    await asyncio.sleep(3)
                    continue

            # Check first if exchanges are both up
            if config['sim_mode_on']:
                exchange_is_up = True
            else:
                exchange_is_up = exchange_up(bnb_exchange)

            if exchange_is_up:
                if not config['sim_mode_on']:
                    # Convert dust to BNB every 6 hours
                    if next_dust_transfer - time.time() < 0:
                        for _ in range(3):
                            try:
                                bnb_account = bnb_exchange.get_account()
                                balances = list(filter(lambda item: float(item['free']) > 0.0 and item['asset'] not in ["USDT", "BNB"], bnb_account['balances']))
                                if balances:
                                    dust_assets = ",".join(list(item['asset'] for item in balances))
                                    transfer_dust = bnb_exchange.transfer_dust(asset=dust_assets)
                                    logger.info(transfer_dust)
                                    next_dust_transfer = time.time() + 21600
                                    if config['telegram_notifications_on']:
                                        telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> Assets dust converted to BNB")
                                break
                            except:
                                logger.info(traceback.format_exc())
                                await asyncio.sleep(2)
                                continue

                # Manage active trades
                for trade in trades:
                    if config['sim_mode_on']:
                        # Get current 4H candle high and low prices of trade['pair']
                        for _ in range(5):
                            try:
                                kline4Hours = bnb_exchange.get_historical_klines(trade['pair'], bnb_exchange.KLINE_INTERVAL_4HOUR, "4 hours ago UTC")
                                kline4HoursHi = float(kline4Hours[0][2])
                                kline4HoursLo = float(kline4Hours[0][3])
                                kline2Hours = bnb_exchange.get_historical_klines(trade['pair'], bnb_exchange.KLINE_INTERVAL_2HOUR, "2 hours ago UTC")
                                kline2HoursHi = float(kline2Hours[0][2])
                                kline2HoursLo = float(kline4Hours[0][3])
                                bnb_tickers = bnb_exchange.get_orderbook_tickers()
                                bnb_ticker = next(pair for pair in bnb_tickers if pair['symbol'] == trade['pair'])
                                break
                            except:
                                logger.info("Retrying...")
                                await asyncio.sleep(3)
                                continue
                        bnb_sell_price = bnb_ticker['askPrice']
                        sell_price = ('%.8f' % float(bnb_sell_price)).rstrip('0').rstrip('.')
                        if trade['interval'] == '4H':
                            klineHi = kline4HoursHi
                        else:
                            klineHi = kline2HoursHi
                        if klineHi >= float(trade['expsellprice']) and time.time() - trade['time'] > 600:
                            # Success!
                            trade['status'] = 'remove'
                            trade['result'] = 'successful'
                            actual_volume = float(trade['expsellprice']) * float(trade['quantity']) * (1.0 - float(config['binance_trade_fee']))
                    else:
                        # Get trade status
                        order = None
                        for _ in range(10):
                            try:
                                order = bnb_exchange.get_order(symbol=trade['pair'], orderId=trade['orderid'])
                                break
                            except:
                                logger.info(traceback.format_exc())
                                await asyncio.sleep(5)
                                continue
                        if order:
                            if order['status'] == "FILLED":
                                trade['status'] = 'remove'
                                trade['result'] = 'successful'
                            elif order['status'] == "EXPIRED":
                                trade['status'] = 'remove'
                                trade['result'] = 'unsuccessful'
                            elif time.time() > trade['expirytime']:
                                # Cancel order and market sell
                                for _ in range(10):
                                    try:
                                        cancel_order = bnb_exchange.cancel_order(symbol=trade['pair'], orderId=trade['orderid'])
                                        break
                                    except:
                                        logger.info(traceback.format_exc())
                                        await asyncio.sleep(5)
                                        continue
                                trade['status'] = 'remove'
                                trade['result'] = 'unsuccessful'
                                # Market sell
                                bnb_balance_result = bnb_exchange.get_asset_balance(asset=trade['pair'].replace('USDT',''))
                                if bnb_balance_result:
                                    bnb_currency_available = bnb_balance_result['free']
                                else:
                                    bnb_currency_available = 0.0
                                info = bnb_exchange.get_symbol_info(trade['pair'])
                                tickSize = info['filters'][0]['tickSize']
                                pair_num_decimals = tickSize.find('1')
                                lotSize = info['filters'][2]['stepSize']
                                if lotSize.find('1') == 0:
                                    lotSize_decimals = 0
                                else:
                                    lotSize_decimals = lotSize.find('1')
                                bnb_currency_available = bnb_currency_available[0:bnb_currency_available.find('.') + lotSize_decimals]
                                result_bnb = None
                                for _ in range(10):
                                    try:
                                        logger.info(f"Attempting market sell {trade['pair']}: Qty = {str(bnb_currency_available)}")
                                        result_bnb = bnb_exchange.order_market_sell(symbol=trade['pair'], quantity=bnb_currency_available)
                                        break
                                    except:
                                        logger.info(traceback.format_exc())
                                        await asyncio.sleep(5)
                                        continue
                                if result_bnb:
                                    logger.info(result_bnb)
                                else:
                                    raise Exception("Could not market sell after order time expired!!")
                    if trade['status'] == 'remove':
                        if config['sim_mode_on']:
                            balance = float(balance) + float(actual_volume)
                            profit = float(actual_volume) - float(config['trade_amount'])
                        else:
                            balance = get_total_usdt_balance(bnb_exchange)
                            profit = round(float(balance) - float(get_balance(config['sheet_id'])), 2)
                        result_text = "won" if profit > 0 else "lost"
                        logger.info(f"<YATB> [{trade['pair']}] ({trade['result'].upper()}) You have {result_text} {str(round(float(profit), 2))} USDT")
                        if config['telegram_notifications_on']:
                            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> [{trade['pair']}] ({trade['result'].upper()}) You have {result_text} {str(round(float(profit), 2))} USDT")
                            # Update google sheet
                        for _ in range(5):
                            try:
                                update_google_sheet(config['sheet_id'], config['range_name'], round(balance, 2), 0)
                                break
                            except:
                                logger.info(traceback.format_exc())
                                await asyncio.sleep(5)
                                continue
                        opp_details = f"{trade['pair']} - {trade['interval']}"
                        for _ in range(5):
                            try:
                                update_google_sheet_opp_details(config['sheet_id'], opp_details)
                                break
                            except:
                                logger.info(traceback.format_exc())
                                await asyncio.sleep(3)
                                continue
                    else:
                        # Current trade is still valid
                        logger.info(f"<YATB> [{trade['pair']}] Trade ongoing:")
                        print(trade)

                # Remove old trade items
                trades = list(filter(lambda item: item['status'] == 'active', trades))
                sim_trades = config['sim_trades'] - len(trades)

                # Update ongoingTradePairs (this is for not repeating a trading pair if we have just traded it)
                currentTime = time.time()
                ongoingTradePairs = list(filter(lambda item: item['expiryTime'] > currentTime, ongoingTradePairs))
                logger.info("ongoingTradePairs ==========>")
                logger.info(ongoingTradePairs)
                excludedPairs = [item['pair'] for item in ongoingTradePairs]

                # Check if there are available trades and if the time to check markets is correct
                # Check every 2 hours except just a few minutes before GM 22:00 so that we make room for 1D candle opps
                if (
                    sim_trades > 0
                    and time.gmtime()[3] % 2 == 1
                    and time.gmtime()[3] != 21
                    and time.gmtime()[4] >= 53
                    and time.gmtime()[4] < 58
                ):
                    # Update google sheet status field
                    dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
                    statusMessage = f"{dateStamp} -- Iteration {iteration}: Looking for opportunities"
                    for _ in range(3):
                        try:
                            update_google_sheet_status(config['sheet_id'], statusMessage)
                            break
                        except:
                            logger.info(traceback.format_exc())
                            await asyncio.sleep(1)
                            continue

                    # Check good opportunities to buy
                    # Get Coin Market Cap data: assets with a negative 24h percentage and a minimum 24h volume of $50 millions
                    logger.info("Checking market status...")

                    url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
                    parameters = {
                        'start':'1',
                        'limit':'32',
                        'convert':'USDT',
                        'market_cap_min': 2000000000,
                        'volume_24h_min': 35000000
                    }
                    headers = {
                        'Accepts': 'application/json',
                        'X-CMC_PRO_API_KEY': config['cmc_api_key']
                    }

                    session = Session()
                    session.headers.update(headers)

                    for _ in range(5):
                        try:
                            response = session.get(url, params=parameters)
                            data = json.loads(response.text)
                            break
                        except (ConnectionError, Timeout, TooManyRedirects) as e:
                            logger.info(e)
                            print("Retrying after 10 seconds...")
                            await asyncio.sleep(10)
                    targets = data['data']
                    sortedTargets = sorted(targets, key=lambda k: k['quote']['USDT']['percent_change_7d'], reverse=False)
                    logger.info(f"{str(len(targets))} target(s)...")

                    logger.info("Calculating current opportunities...")
                    opps = []
                    for item in sortedTargets:
                        pair = item['symbol'] + "USDT"
                        if pair in usdt_tickers and pair not in excludedPairs:
                            # Initialize variables
                            kline1DayRatio = None
                            kline12HoursRatio = None
                            kline4HoursRatio = None
                            kline2HoursRatio = None
                            prev4HKlineClose = None
                            prev4HKlineLow = None
                            this4HKlineClose = None
                            this4HKlineLow = None
                            prev4HRsi = None
                            this4HRsi = None
                            prev4HBolingerLowBand = None
                            this4HBolingerLowBand = None
                            this4HBolingerMidBand = None
                            prev4HStochFFastK = None
                            prev4HStochFFastD = None
                            this4HStochFFastK = None
                            this4HStochFFastD = None
                            this1DRsi = None
                            prev1DStochFFastK = None
                            prev1DStochFFastD = None
                            this1DStochFFastK = None
                            this1DStochFFastD = None
                            this12HRsi = None
                            prev12HStochFFastK = None
                            prev12HStochFFastD = None
                            this12HStochFFastK = None
                            this12HStochFFastD = None
                            this2HRsi = None
                            this2HStochFFastK = None
                            this2HStochFFastD = None
                            prev2HStochFFastD = None
                            del kline1DayRatio
                            del kline12HoursRatio
                            del kline4HoursRatio
                            del kline2HoursRatio
                            del prev4HKlineClose
                            del prev4HKlineLow
                            del this4HKlineClose
                            del this4HKlineLow
                            del prev4HRsi
                            del this4HRsi
                            del prev4HBolingerLowBand
                            del this4HBolingerLowBand
                            del this4HBolingerMidBand
                            del prev4HStochFFastK
                            del prev4HStochFFastD
                            del this4HStochFFastK
                            del this4HStochFFastD
                            del this1DRsi
                            del prev1DStochFFastK
                            del prev1DStochFFastD
                            del this1DStochFFastK
                            del this1DStochFFastD
                            del this12HRsi
                            del prev12HStochFFastK
                            del prev12HStochFFastD
                            del this12HStochFFastK
                            del this12HStochFFastD
                            del this2HRsi
                            del this2HStochFFastK
                            del this2HStochFFastD
                            del prev2HStochFFastD

                            # Get 1 day tech info if the 1d candle is about to finish
                            if time.gmtime()[3] % 24 == 23 and time.gmtime()[4] >= 53:
                                for _ in range(5):
                                    try:
                                        this1DKlineClose = None
                                        this1DKlineLow = None
                                        klines1Day = bnb_exchange.get_historical_klines(pair, bnb_exchange.KLINE_INTERVAL_1DAY, "1 day ago UTC")
                                        this1DKlineClose = float(klines1Day[0][4])
                                        this1DKlineLow = float(klines1Day[0][3])
                                        kline1DayRatio = this1DKlineClose/this1DKlineLow
                                        break
                                    except:
                                        logger.info(f"Retrying... ({pair})")
                                        await asyncio.sleep(2)
                                        continue
                                taapiSymbol = pair.split('USDT')[0] + "/" + "USDT"
                                endpoint = "https://api.taapi.io/bulk"

                                parameters = {
                                    "secret": config['taapi_api_key'],
                                    "construct": {
                                        "exchange": "binance",
                                        "symbol": taapiSymbol,
                                        "interval": "1d",
                                        "indicators": [
                                        {
                                            # Current Relative Strength Index
                                            "id": "thisrsi",
                                	        "indicator": "rsi"
                                        },
                                        {
                                            # Current stoch fast
                                            "id": "thisstochf",
                                            "indicator": "stochf",
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        },
                                        {
                                            # Previous stoch fast
                                            "id": "prevstochf",
                                            "indicator": "stochf",
                                            "backtrack": 1,
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        }
                                        ]
                                    }
                                }

                                for _ in range(5):
                                    try:
                                        # Send POST request and save the response as response object
                                        response = requests.post(url = endpoint, json = parameters)

                                        # Extract data in json format
                                        result = response.json()

                                        this1DRsi = float(result['data'][0]['result']['value'])
                                        this1DStochFFastK = float(result['data'][1]['result']['valueFastK'])
                                        this1DStochFFastD = float(result['data'][1]['result']['valueFastD'])
                                        prev1DStochFFastK = float(result['data'][2]['result']['valueFastK'])
                                        prev1DStochFFastD = float(result['data'][2]['result']['valueFastD'])
                                        await asyncio.sleep(2)
                                        break
                                    except:
                                        logger.info(f"{pair} | TAAPI Response (1D): {response.reason}. Trying again...")
                                        await asyncio.sleep(2)
                                        continue

                            # Get 12 Hours tech info if the 12 Hours candle is about to finish
                            if time.gmtime()[3] % 12 == 11 and time.gmtime()[4] >= 53:
                                for _ in range(5):
                                    try:
                                        this12HKlineClose = None
                                        this12HKlineLow = None
                                        klines12Hours = bnb_exchange.get_historical_klines(pair, bnb_exchange.KLINE_INTERVAL_12HOUR, "12 hours ago UTC")
                                        this12HKlineClose = float(klines12Hours[0][4])
                                        this12HKlineLow = float(klines12Hours[0][3])
                                        kline12HoursRatio = this12HKlineClose/this12HKlineLow
                                        break
                                    except:
                                        logger.info(f"Retrying... ({pair})")
                                        await asyncio.sleep(2)
                                        continue
                                taapiSymbol = pair.split('USDT')[0] + "/" + "USDT"
                                endpoint = "https://api.taapi.io/bulk"

                                parameters = {
                                    "secret": config['taapi_api_key'],
                                    "construct": {
                                        "exchange": "binance",
                                        "symbol": taapiSymbol,
                                        "interval": "12h",
                                        "indicators": [
                                        {
                                            # Current Relative Strength Index
                                            "id": "thisrsi",
                                	        "indicator": "rsi"
                                        },
                                        {
                                            # Current stoch fast
                                            "id": "thisstochf",
                                            "indicator": "stochf",
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        },
                                        {
                                            # Previous stoch fast
                                            "id": "prevstochf",
                                            "indicator": "stochf",
                                            "backtrack": 1,
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        }
                                        ]
                                    }
                                }

                                for _ in range(5):
                                    try:
                                        # Send POST request and save the response as response object
                                        response = requests.post(url = endpoint, json = parameters)

                                        # Extract data in json format
                                        result = response.json()

                                        this12HRsi = float(result['data'][0]['result']['value'])
                                        this12HStochFFastK = float(result['data'][1]['result']['valueFastK'])
                                        this12HStochFFastD = float(result['data'][1]['result']['valueFastD'])
                                        prev12HStochFFastK = float(result['data'][2]['result']['valueFastK'])
                                        prev12HStochFFastD = float(result['data'][2]['result']['valueFastD'])
                                        await asyncio.sleep(2)
                                        break
                                    except:
                                        logger.info(f"{pair} | TAAPI Response (12H): {response.reason}. Trying again...")
                                        await asyncio.sleep(2)
                                        continue

                            # Get 4 hours tech info if the 4 hours candle is about to finish
                            if time.gmtime()[3] % 4 == 3 and time.gmtime()[4] >= 53:
                                for _ in range(5):
                                    try:
                                        klines4Hours = bnb_exchange.get_historical_klines(pair, bnb_exchange.KLINE_INTERVAL_4HOUR, "12 hours ago UTC")
                                        if float(klines4Hours[0][4]) - float(klines4Hours[0][1]) < 0:
                                            beforePrev4HKline = 'negative'
                                        else:
                                            beforePrev4HKline = 'positive'
                                        if float(klines4Hours[1][4]) - float(klines4Hours[1][1]) < 0:
                                            prev4HKline = 'negative'
                                        else:
                                            prev4HKline = 'positive'
                                        if float(klines4Hours[2][4]) - float(klines4Hours[2][1]) < 0:
                                            this4HKline = 'negative'
                                        else:
                                            this4HKline = 'positive'
                                        prev4HKlineClose = float(klines4Hours[1][4])
                                        prev4HKlineLow = float(klines4Hours[1][3])
                                        this4HKlineClose = float(klines4Hours[2][4])
                                        this4HKlineLow = float(klines4Hours[2][3])
                                        kline4HoursRatio = this4HKlineClose/this4HKlineLow
                                        break
                                    except:
                                        logger.info(f"Retrying... ({pair})")
                                        await asyncio.sleep(2)
                                        continue

                                # Get technical info
                                taapiSymbol = pair.split('USDT')[0] + "/" + "USDT"
                                endpoint = "https://api.taapi.io/bulk"

                                # Get 4H indicators
                                parameters = {
                                    "secret": config['taapi_api_key'],
                                    "construct": {
                                        "exchange": "binance",
                                        "symbol": taapiSymbol,
                                        "interval": "4h",
                                        "indicators": [
                                	    {
                                            # Previous Relative Strength Index
                                            "id": "prevrsi",
                                	        "indicator": "rsi",
                                            "backtrack": 1
                                	    },
                                        {
                                            # Current Relative Strength Index
                                            "id": "thisrsi",
                                	        "indicator": "rsi"
                                        },
                                        {
                                            # Previous Bolinger bands
                                            "id": "prevbb",
                                            "indicator": "bbands2",
                                            "backtrack": 1
                                        },
                                        {
                                            # Current Bolinger bands
                                            "id": "thisbb",
                                            "indicator": "bbands2"
                                        },
                                        {
                                            # Previous stoch fast
                                            "id": "prevstochf",
                                            "indicator": "stochf",
                                            "backtrack": 1,
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        },
                                        {
                                            # Current stoch fast
                                            "id": "thisstochf",
                                            "indicator": "stochf",
                                            "optInFastK_Period": 3,
                                            "optInFastD_Period": 3
                                        }
                                        ]
                                    }
                                }

                                for _ in range(5):
                                    try:
                                        # Send POST request and save the response as response object
                                        response = requests.post(url = endpoint, json = parameters)

                                        # Extract data in json format
                                        result = response.json()

                                        prev4HRsi = float(result['data'][0]['result']['value'])
                                        this4HRsi = float(result['data'][1]['result']['value'])
                                        prev4HBolingerLowBand = float(result['data'][2]['result']['valueLowerBand'])
                                        this4HBolingerLowBand = float(result['data'][3]['result']['valueLowerBand'])
                                        this4HBolingerMidBand = float(result['data'][3]['result']['valueMiddleBand'])
                                        prev4HStochFFastK = float(result['data'][4]['result']['valueFastK'])
                                        prev4HStochFFastD = float(result['data'][4]['result']['valueFastD'])
                                        this4HStochFFastK = float(result['data'][5]['result']['valueFastK'])
                                        this4HStochFFastD = float(result['data'][5]['result']['valueFastD'])
                                        await asyncio.sleep(2)
                                        break
                                    except:
                                        logger.info(f"{pair} | TAAPI Response (4H): {response.reason}. Trying again...")
                                        await asyncio.sleep(2)
                                        continue
                            # end if time.gmtime()[3] % 4 == 3 and time.gmtime()[4] >= 53

                            # Get 2H indicators when the 2 hours candle is about to finish
                            # Define a JSON body with parameters to be sent to the API
                            for _ in range(5):
                                try:
                                    this2HKlineClose = None
                                    this2HKlineLow = None
                                    klines2Hours = bnb_exchange.get_historical_klines(pair, bnb_exchange.KLINE_INTERVAL_2HOUR, "2 hours ago UTC")
                                    this2HKlineClose = float(klines2Hours[0][4])
                                    this2HKlineLow = float(klines2Hours[0][3])
                                    kline2HoursRatio = this2HKlineClose/this2HKlineLow
                                    break
                                except:
                                    logger.info(f"Retrying... ({pair})")
                                    await asyncio.sleep(2)
                                    continue
                            taapiSymbol = pair.split('USDT')[0] + "/" + "USDT"
                            endpoint = "https://api.taapi.io/bulk"

                            parameters = {
                                "secret": config['taapi_api_key'],
                                "construct": {
                                    "exchange": "binance",
                                    "symbol": taapiSymbol,
                                    "interval": "2h",
                                    "indicators": [
                                    {
                                        # Current Relative Strength Index
                                        "id": "thisrsi",
                            	        "indicator": "rsi"
                                    },
                                    {
                                        # Current stoch fast
                                        "id": "thisstochf",
                                        "indicator": "stochf",
                                        "optInFastK_Period": 3,
                                        "optInFastD_Period": 3
                                    },
                                    {
                                        # Previous stoch fast
                                        "id": "prevstochf",
                                        "indicator": "stochf",
                                        "backtrack": 1,
                                        "optInFastK_Period": 3,
                                        "optInFastD_Period": 3
                                    },
                                    {
                                        # Current Bolinger bands
                                        "id": "thisbb",
                                        "indicator": "bbands2"
                                    }
                                    ]
                                }
                            }

                            for _ in range(5):
                                try:
                                    # Send POST request and save the response as response object
                                    response = requests.post(url = endpoint, json = parameters)

                                    # Extract data in json format
                                    result = response.json()

                                    this2HRsi = float(result['data'][0]['result']['value'])
                                    this2HStochFFastK = float(result['data'][1]['result']['valueFastK'])
                                    this2HStochFFastD = float(result['data'][1]['result']['valueFastD'])
                                    prev2HStochFFastD = float(result['data'][2]['result']['valueFastD'])
                                    this2HBolinger = float(result['data'][3]['result']['valueLowerBand'])
                                    await asyncio.sleep(2)
                                    break
                                except:
                                    logger.info(f"{pair} | TAAPI Response (2H): {response.reason}. Trying again...")
                                    await asyncio.sleep(2)
                                    continue

                            try:
                                if time.gmtime()[3] % 24 == 23 and time.gmtime()[4] >= 53:
                                    logger.info(f"1D Data: {pair} | kline1DayRatio {str(round(kline1DayRatio, 4))} | This RSI {str(round(this1DRsi, 2))} | Prev StochF K,D {str(round(prev1DStochFFastK, 2))}, {str(round(prev1DStochFFastD, 2))} | This StochF K,D {str(round(this1DStochFFastK, 2))}|{str(round(this1DStochFFastD, 2))}")
                                if time.gmtime()[3] % 12 == 11 and time.gmtime()[4] >= 53:
                                    logger.info(f"12H Data: {pair} | kline12HoursRatio {str(round(kline12HoursRatio, 4))} | This RSI {str(round(this12HRsi, 2))} | Prev StochF K,D {str(round(prev12HStochFFastK, 2))}, {str(round(prev12HStochFFastD, 2))} | This StochF K,D {str(round(this12HStochFFastK, 2))}|{str(round(this12HStochFFastD, 2))}")
                                if time.gmtime()[3] % 4 == 3 and time.gmtime()[4] >= 53:
                                    logger.info(f"4H Data: {pair} | kline4HoursRatio {str(round(kline4HoursRatio, 4))} | Prev Kline {str(prev4HKline)} | This Kline {str(this4HKline)} | Prev Kline Low {str(prev4HKlineLow)} | Prev Lower Bolinger {str(prev4HBolingerLowBand)} | Prev RSI {str(round(prev4HRsi, 2))} | This RSI {str(round(this4HRsi, 2))} | Prev StochF K,D {str(round(prev4HStochFFastK, 2))}|{str(round(prev4HStochFFastD, 2))} | This StochF K,D {str(round(this4HStochFFastK, 2))}|{str(round(this4HStochFFastD, 2))}")
                                logger.info(f"2H Data: {pair} | kline2HoursRatio {str(round(kline2HoursRatio, 4))} | This RSI {str(round(this2HRsi, 2))} | Prev StochF D {str(round(prev2HStochFFastD, 2))} | This StochF K,D {str(round(this2HStochFFastK, 2))}|{str(round(this2HStochFFastD, 2))} | This Lower Bolinger {str(round(this2HBolinger, 4))}")
                                now = datetime.datetime(time.gmtime()[0], time.gmtime()[1], time.gmtime()[2], time.gmtime()[3])

                                if (
                                    time.gmtime()[3] % 24 == 23
                                    and time.gmtime()[4] >= 53
                                    and this1DRsi < 43.0
                                    and this1DStochFFastK < 14.5
                                    and this1DStochFFastK < this1DStochFFastD
                                    and this1DStochFFastK + 10.0 < this1DStochFFastD
                                    and prev1DStochFFastK < 78.0
                                    and prev1DStochFFastK < prev1DStochFFastD
                                    and prev1DStochFFastK - this1DStochFFastK < 20.0
                                    and kline1DayRatio > 1.006
                                    and sim_trades > 0
                                ):
                                    # Put 1D opportunities in opps dict
                                    opps.append({'pair': pair, 'interval': "1d", 'priority': 1})
                                    logger.info(f"{pair} good candidate for the 1 day strategy")
                                elif (
                                    time.gmtime()[3] % 12 == 11
                                    and time.gmtime()[4] >= 53
                                    and this12HRsi < 45.0
                                    and this12HStochFFastK < 14.5
                                    and this12HStochFFastK < this12HStochFFastD
                                    and this12HStochFFastK + 10.0 < this12HStochFFastD
                                    and prev12HStochFFastK < 78.0
                                    and prev12HStochFFastK < prev12HStochFFastD
                                    and prev12HStochFFastK - this12HStochFFastK < 20.0
                                    and kline12HoursRatio > 1.003
                                    and sim_trades > 0
                                ):
                                    # Put 12H opportunities in opps dict
                                    opps.append({'pair': pair, 'interval': "12h", 'priority': 2})
                                    logger.info(f"{pair} good candidate for the 12h strategy")
                                elif (
                                    (time.gmtime()[3] % 4 == 3
                                    and time.gmtime()[4] >= 53
                                    and this4HKline == 'negative'
                                    and float(this4HKlineClose) < float(this4HBolingerLowBand)
                                    and this4HRsi < 30.0
                                    and kline4HoursRatio > 1.0029
                                    and sim_trades > 0)
                                    or (time.gmtime()[3] % 4 == 3
                                        and time.gmtime()[4] >= 53
                                        and prev4HKline == 'negative'
                                        and this4HKline == 'positive'
                                        and float(this4HKlineClose) < float(this4HBolingerLowBand)
                                        and float(this4HKlineClose) < float(this4HBolingerMidBand)
                                        and sim_trades > 0
                                    )
                                    or (
                                        (time.gmtime()[3] % 4 == 3
                                        and time.gmtime()[4] >= 53
                                        and prev4HStochFFastK < prev4HStochFFastD
                                        and this4HStochFFastK > this4HStochFFastD
                                        and this4HStochFFastK > 75.0
                                        and this4HStochFFastK < 99.0
                                        and this4HStochFFastK - this4HStochFFastD > (this4HStochFFastK * 0.275)
                                        and this4HRsi < 55.0
                                        and sim_trades > 0)
                                    )
                                ):
                                    # Put 4H opportunities in opps dict
                                    opps.append({'pair': pair, 'interval': "4h", 'priority': 3})
                                    logger.info(f"{pair} good candidate for one of the 4H strategies")
                                elif (
                                    time.gmtime()[3] % 2 == 1
                                    and time.gmtime()[4] >= 53
                                    and this2HRsi < 35.0
                                    and this2HStochFFastK < 13.0
                                    and this2HStochFFastK < this2HStochFFastD
                                    and (this2HStochFFastK + 10.0 < this2HStochFFastD
                                        and this2HStochFFastD > prev2HStochFFastD - 15.0
                                        and this2HStochFFastD > 28.0
                                        and prev2HStochFFastD > 28.0)
                                    and not twoh_trading_break(now)
                                    and kline2HoursRatio > 1.002
                                    and sim_trades > 0
                                ):
                                    # Put 2H opportunities in opps dict
                                    opps.append({'pair': pair, 'interval': "2h", 'priority': 4})
                                    logger.info(f"{pair} good candidate for 2H low Stochastic Fast K stategy")
                                else:
                                    logger.info(f"{pair} not a good entry point")
                            except:
                                logger.info(traceback.format_exc())
                                logger.info(f"Problem with pair {pair}")
                    # Sort opportunities based on priority
                    opps = sorted(opps, key=lambda k: k['priority'])
                    logger.info("Opps ==========>")
                    logger.info(opps)
                    if opps:
                        seconds_to_candle_end = 0
                        if opps[0]['interval'] == '2h' and time.gmtime()[4] > 50 and time.gmtime()[4] < 59:
                            logger.info("Waiting until 90 seconds before candle close time to re-check indicators")
                            if time.gmtime()[3] < 23:
                                candle_end = datetime.datetime(time.gmtime()[0], time.gmtime()[1], time.gmtime()[2], time.gmtime()[3] + 1)
                            else:
                                tomorrow = datetime.datetime(time.gmtime()[0], time.gmtime()[1], time.gmtime()[2]) + timedelta(hours=24)
                                candle_end = datetime.datetime(tomorrow.year, tomorrow.month, tomorrow.day)
                            now = datetime.datetime(time.gmtime()[0], time.gmtime()[1], time.gmtime()[2], time.gmtime()[3],time.gmtime()[4], time.gmtime()[5])
                            seconds_to_candle_end = (candle_end - now).seconds
                            # Update google sheet status field
                            dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
                            statusMessage = f"{dateStamp} -- Iteration {iteration}: Waiting until 90 seconds before candle close"
                            for _ in range(3):
                                try:
                                    update_google_sheet_status(config['sheet_id'], statusMessage)
                                    break
                                except:
                                    logger.info(traceback.format_exc())
                                    await asyncio.sleep(1)
                                    continue
                            if seconds_to_candle_end > 90:
                                await asyncio.sleep(seconds_to_candle_end - 90)
                        for opp in opps:
                            if sim_trades > 0:
                                # If 2h opp, check again if techincal info is still ok; if not, skip this opp
                                if opp['interval'] == '2h' and seconds_to_candle_end > 90:
                                    two_hours_tech_info = get_2h_tech_info(opp['pair'])
                                    logger.info(f"2H Data: {opp['pair']} | This RSI {str(round(two_hours_tech_info[0], 2))} | Prev StochF D {str(round(two_hours_tech_info[3], 2))} | This StochF K,D {str(round(two_hours_tech_info[1], 2))}|{str(round(two_hours_tech_info[2], 2))} | This Lower Bolinger {str(round(two_hours_tech_info[3], 4))}")
                                    if all(two_hours_tech_info):
                                        if not (
                                            two_hours_tech_info[0] < 69.0
                                            and two_hours_tech_info[0] > 40.0
                                            and two_hours_tech_info[1] < 14.5
                                            and two_hours_tech_info[1] < two_hours_tech_info[2]
                                            and (two_hours_tech_info[1] + 10.0 < two_hours_tech_info[2]
                                                and two_hours_tech_info[2] > two_hours_tech_info[3] - 15.0
                                                and two_hours_tech_info[2] > 28.0
                                                and two_hours_tech_info[3] > 28.0)
                                        ):
                                            logger.info(f"{opp['pair']}: Not a good chance in the end")
                                            continue
                                    else:
                                        logger.info("Could not verify if the 2H opportunity is still good near candle close time")
                                        continue
                                ongoingTradePairs.append({'pair': opp['pair'], 'expiryTime': time.time() + 5400.0})
                                bnb_buy_price = None
                                del bnb_buy_price
                                for _ in range(5):
                                    try:
                                        bnb_tickers = bnb_exchange.get_orderbook_tickers()
                                        bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == opp['pair'])
                                        bnb_buy_price = bnb_ticker['bidPrice']
                                        break
                                    except:
                                        logger.info(f"{opp['pair']} | Re-trying ticker...")
                                        await asyncio.sleep(2)
                                        continue
                                if opp['interval'] == "1d":
                                    expectedProfitPercentage = 1.007
                                    stopPrice = float(bnb_buy_price) * 0.9855
                                    stopLimitPrice = float(bnb_buy_price) * 0.985
                                elif opp['interval'] == "12h":
                                    expectedProfitPercentage = 1.004
                                    stopPrice = float(bnb_buy_price) * 0.9855
                                    stopLimitPrice = float(bnb_buy_price) * 0.985
                                elif opp['interval'] == "4h":
                                    expectedProfitPercentage = 1.0032
                                    stopPrice = float(bnb_buy_price) * 0.9905
                                    stopLimitPrice = float(bnb_buy_price) * 0.99
                                else:
                                    expectedProfitPercentage = 1.0023
                                    stopPrice = float(bnb_buy_price) * 0.9905
                                    stopLimitPrice = float(bnb_buy_price) * 0.99
                                # buy_price = ('%.8f' % float(bnb_buy_price)).rstrip('0').rstrip('.')
                                info = bnb_exchange.get_symbol_info(opp['pair'])
                                tickSize = info['filters'][0]['tickSize']
                                pair_num_decimals = tickSize.find('1')
                                lotSize = info['filters'][2]['stepSize']
                                if lotSize.find('1') == 0:
                                    lotSize_decimals = 0
                                else:
                                    lotSize_decimals = lotSize.find('1')
                                expSellPrice = float(bnb_buy_price) * expectedProfitPercentage
                                expSellPrice = ("%.17f" % expSellPrice).rstrip('0').rstrip('.')
                                expSellPrice = expSellPrice[0:expSellPrice.find('.') + pair_num_decimals]
                                stopPrice = ("%.17f" % stopPrice).rstrip('0').rstrip('.')
                                stopPrice = stopPrice[0:stopPrice.find('.') + pair_num_decimals]
                                stopLimitPrice = ("%.17f" % stopLimitPrice).rstrip('0').rstrip('.')
                                stopLimitPrice = stopLimitPrice[0:stopLimitPrice.find('.') + pair_num_decimals]
                                expBuyPrice = str(bnb_buy_price)
                                expBuyPrice = float(expBuyPrice[0:expBuyPrice.find('.') + pair_num_decimals])
                                stopLoss = float(bnb_buy_price) - ((float(expSellPrice) - float(bnb_buy_price)) * 1.5)
                                # fExpSellPrice = ('%.8f' % expSellPrice).rstrip('0').rstrip('.')
                                if config['sim_mode_on']:
                                    # Simulate buy order
                                    quantity = round((float(config['trade_amount']) / float(bnb_buy_price)) * (1.0 - float(config['binance_trade_fee'])), 5)
                                    profit = round((float(quantity) * float(bnb_buy_price) * expectedProfitPercentage * (1.0 - float(config['binance_trade_fee'])) - config['trade_amount']), 3)
                                    trades.append({'pair': opp['pair'], 'type': 'sim', 'interval': opp['interval'], 'status': 'active', 'orderid': 0, 'time': time.time(), 'expirytime': time.time() + 43200.0, 'buyprice': float(bnb_buy_price), 'expsellprice': expSellPrice, 'stoploss': stopLoss, 'quantity': quantity})
                                    logger.info(f"<YATB SIM> [{opp['pair']}] Bought {str(config['trade_amount'])} @ {bnb_buy_price} = {str(quantity)}. Sell @ {str(expSellPrice)}. Exp. Profit ~ {str(profit)} USDT")
                                    if config['telegram_notifications_on']:
                                        telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB SIM> [{opp['pair']}] Bought {str(config['trade_amount'])} @ {bnb_buy_price} = {str(quantity)}. Sell @ {str(expSellPrice)}. Exp. Profit ~ {str(profit)} USDT")
                                    balance = float(balance) - float(config['trade_amount'])
                                    sim_trades -= 1
                                else:
                                    # Market buy order
                                    bnb_balance_result = bnb_exchange.get_asset_balance(asset='USDT')
                                    if bnb_balance_result:
                                        bnb_currency_available = bnb_balance_result['free']
                                    else:
                                        bnb_currency_available = 0.0
                                    result_bnb = None
                                    for _ in range(10):
                                        try:
                                            logger.info(f"Attempting market order {opp['pair']}: quoteOrderQty = {str(bnb_currency_available)}")
                                            result_bnb = bnb_exchange.order_market_buy(symbol=opp['pair'], quoteOrderQty=bnb_currency_available)
                                            # trades.append({'pair': opp['pair'], 'type': 'real', 'interval': opp['interval'], 'status': 'active', 'orderid': 0, 'time': time.time(), 'expirytime': time.time() + 43200.0, 'buyprice': float(bnb_buy_price), 'expsellprice': expSellPrice, 'stoploss': stopLoss, 'quantity': quantity})
                                            break
                                        except:
                                            logger.info(traceback.format_exc())
                                            continue
                                    if result_bnb:
                                        logger.info(result_bnb)
                                        if config['telegram_notifications_on']:
                                            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> [{opp['pair']} ({opp['interval']}]) Bought {str(bnb_currency_available)} USDT @ {str(result_bnb['fills'][0]['price'])} Selling @ {str(expSellPrice)}")

                                    # # Buy order (limit)
                                    # bnb_balance_result = bnb_exchange.get_asset_balance(asset='USDT')
                                    # if bnb_balance_result:
                                    #     bnb_currency_available = bnb_balance_result['free']
                                    # else:
                                    #     bnb_currency_available = 0.0
                                    # price = str(expBuyPrice - (float(tickSize) * 5))
                                    # quantity = str((float(bnb_currency_available) - 1) / float(price))
                                    # quantity = quantity[0:quantity.find('.') + lotSize_decimals]
                                    # profit = round((float(quantity) * float(price) * expectedProfitPercentage * (1.0 - float(config['binance_trade_fee'])) - float(bnb_currency_available)), 3)
                                    # result_bnb = None
                                    # for _ in range(10):
                                    #     try:
                                    #         result_bnb = bnb_exchange.order_limit_buy(symbol=opp['pair'], quantity=quantity, price=price)
                                    #         logger.info(result_bnb)
                                    #         # trades.append({'pair': opp['pair'], 'type': 'real', 'interval': opp['interval'], 'status': 'active', 'orderid': 0, 'time': time.time(), 'expirytime': time.time() + 43200.0, 'buyprice': float(bnb_buy_price), 'expsellprice': expSellPrice, 'stoploss': stopLoss, 'quantity': quantity})
                                    #         break
                                    #     except:
                                    #         logger.info(traceback.format_exc())
                                    #         continue
                                    # if result_bnb:
                                    #     # Wait 10 seconds to give exchanges time to process orders
                                    #     logger.info('Waiting 20 seconds to give exchanges time to process orders...')
                                    #     if config['telegram_notifications_on']:
                                    #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB SIM> [{opp['pair']}] Limit order sent ({opp['interval']} opp). Buying {str(quantity)} @ {str(price)}. Selling @ {str(expSellPrice)}. Exp. Profit ~ {str(profit)} USDT")
                                    #     await asyncio.sleep(20)
                                    #
                                    # ################################################################################################################################################
                                    # # Wait until limit orders have been fulfilled
                                    # ################################################################################################################################################
                                    # limit_orders_closed = await wait_for_bnb_order(config, bnb_exchange, logger)
                                    # if not limit_orders_closed:
                                    #     # if config['telegram_notifications_on']:
                                    #     #     telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): At least 1 limit order could not be fulfilled after {config['limit_order_time']} seconds. You must find a recover way!")
                                    #     # raise Exception("Limit orders not fulfilled!")
                                    #
                                    #     # Cancel open orders
                                    #     for _ in range (10):
                                    #         try:
                                    #             result_bnb = bnb_exchange.cancel_order(symbol=opp['pair'], orderId=result_bnb['orderId'])
                                    #             logger.info(result_bnb)
                                    #             break
                                    #         except:
                                    #             logger.info(traceback.format_exc())
                                    #             # wait a few seconds before trying again
                                    #             await asyncio.sleep(2)
                                    #             continue
                                    #
                                    #     # Re-try trade until success
                                    #     tries = 100
                                    #     success = False
                                    #     first_try = True
                                    #     while tries >= 0 and not success:
                                    #         try:
                                    #             logger.info("Trying to limit buy in Binance...")
                                    #             tries -= 1
                                    #
                                    #             # calculate trade_amount
                                    #             bnb_balance_result = bnb_exchange.get_asset_balance(asset='USDT')
                                    #             if bnb_balance_result:
                                    #                 bnb_currency_available = bnb_balance_result['free']
                                    #             else:
                                    #                 bnb_currency_available = 0.0
                                    #
                                    #             # Get latest ticker info
                                    #             bnb_tickers = bnb_exchange.get_orderbook_tickers()
                                    #             bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == opp['pair'])
                                    #             bnb_buy_price = bnb_ticker['bidPrice']
                                    #             expBuyPrice = str(bnb_buy_price)
                                    #             expBuyPrice = float(expBuyPrice[0:expBuyPrice.find('.') + pair_num_decimals])
                                    #             price = str(expBuyPrice - (float(tickSize) * 3))
                                    #             quantity = str((float(bnb_currency_available) - 1) / float(price))
                                    #             quantity = quantity[0:quantity.find('.') + lotSize_decimals]
                                    #             # if usdt_already_spent >= (float(config['trade_amount']) - 60) and not first_try:
                                    #             if float(quantity) <= 5.0 and not first_try:
                                    #                 success = True
                                    #             else:
                                    #                 result_bnb = None
                                    #                 result_bnb = bnb_exchange.order_limit_buy(symbol=opp['pair'], quantity=quantity, price=str(price))
                                    #                 logger.info(result_bnb)
                                    #                 # trades.append({'exchange': 'bnb', 'orderid': result_bnb['orderId'], 'time': time.time(), 'spread': item['spread']})
                                    #                 if result_bnb:
                                    #                     first_try = False
                                    #                 limit_order_successful = await short_wait_for_bnb_order(opp['pair'], result_bnb['orderId'], config, bnb_exchange, logger)
                                    #                 success = limit_order_successful
                                    #         except:
                                    #             logger.info(traceback.format_exc())
                                    #             # wait a few seconds before trying again
                                    #             await asyncio.sleep(2)
                                    #             continue
                                    #
                                    #     if not success:
                                    #         if config['telegram_notifications_on']:
                                    #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> Error when buying in Binance")
                                    #         raise Exception(f"Error when buying in Binance")
                                    #     else:
                                        # Create limit sell order
                                        for _ in range(30):
                                            try:
                                                # calculate expiry time (let the trade go for a maximum of 3 candle sticks or 1 if it's 1d)
                                                expiry_time = time.time() + 43200.0
                                                if opp['interval'] == '1d' or opp['interval'] == '12h':
                                                    expiry_time = time.time() + 57600.0
                                                elif opp['interval'] == '2h':
                                                    expiry_time = time.time() + 21600.0
                                                # calculate trade_amount
                                                bnb_balance_result = bnb_exchange.get_asset_balance(asset=opp['pair'].replace('USDT',''))
                                                if bnb_balance_result:
                                                    bnb_currency_available = bnb_balance_result['free']
                                                else:
                                                    bnb_currency_available = 0.0
                                                quantity = bnb_currency_available[0:bnb_currency_available.find('.') + lotSize_decimals]
                                                logger.info(f"Attempting limit order {opp['pair']}: quantity = {str(quantity)}; price = {str(expSellPrice)}")
                                                result_bnb = None
                                                result_bnb = bnb_exchange.order_limit_sell(symbol=opp['pair'], quantity=quantity, price=expSellPrice)
                                                # result_bnb = bnb_exchange.order_oco_sell(
                                                #     symbol=opp['pair'],
                                                #     quantity=quantity,
                                                #     price=expSellPrice,
                                                #     stopPrice=stopPrice,
                                                #     stopLimitPrice=stopLimitPrice,
                                                #     stopLimitTimeInForce="GTC")
                                                logger.info(result_bnb)
                                                # order = list(filter(lambda item: item['type'] == "LIMIT_MAKER", result_bnb['orderReports']))[0]
                                                trades.append({'pair': opp['pair'], 'type': 'real', 'interval': opp['interval'], 'status': 'active', 'orderid': result_bnb['orderId'], 'time': time.time(), 'expirytime': expiry_time, 'buyprice': float(bnb_buy_price), 'expsellprice': expSellPrice, 'stoploss': stopLimitPrice, 'quantity': quantity})
                                                sim_trades -= 1
                                                break
                                            except:
                                                logger.info(traceback.format_exc())
                                                await asyncio.sleep(3)
                                                continue
                                        if result_bnb:
                                            logger.info(result_bnb)
                                            if config['telegram_notifications_on']:
                                                telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> [{opp['pair']} ({opp['interval']}]) Limit sell order created. Waiting...")
                                        else:
                                            if config['telegram_notifications_on']:
                                                telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> Could not create sell order in Binance!!")
                                            raise Exception(f"Could not create sell order in Binance!!")
                                    else:
                                        logger.info(f"{opp['pair']} Could not market buy. Trying next opp...")
                                        continue
                        # Print opps in google sheet for reference
                        update_google_sheet_opps(config['sheet_id'], str(opps))
                else: #if sim_trades > 0 and time.localtime()[3] % 4 == 3 and time.localtime()[4] > 30:
                    logger.info("Waiting for the right time...")
            else: # if exchange_is_up:
                logger.info("One of the exchanges was down or under maintenance!")


            logger.info(f'------------ Iteration {iteration} ------------\n')

            if config['test_mode_on']:
                await asyncio.sleep(1)
                break
            else:
                # Update google sheet status field
                dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
                statusMessage = f"{dateStamp} -- Iteration {iteration}: Waiting for next iteration"
                if trades:
                    ongoing_trades = list(trade['pair'] + " " + trade['interval'] for trade in trades)
                    statusMessage += f" (Ongoing trades: {ongoing_trades})"
                for _ in range(3):
                    try:
                        update_google_sheet_status(config['sheet_id'], statusMessage)
                        break
                    except:
                        logger.info(traceback.format_exc())
                        await asyncio.sleep(1)
                        continue
                # Wait given seconds until next poll
                logger.info("Waiting for next iteration... ({} seconds)\n\n\n".format(config['seconds_between_iterations']))
                await asyncio.sleep(config['seconds_between_iterations'])
        except Exception as e:
            logger.info(traceback.format_exc())
            # Network issue(s) occurred (most probably). Jumping to next iteration
            logger.info("Exception occurred -> '{}'. Waiting for next iteration... ({} seconds)\n\n\n".format(e, config['seconds_between_iterations']))
            await asyncio.sleep(config['seconds_between_iterations'])


        #
        #
        #
        #
        #
        #
        #         # # Kraken: Get my balances
        #         kraken_balances = get_kraken_balances(krk_exchange, config)
        #         # logger.info(f"Kraken's Balances\n(Base) {config['krk_base_currency']} balance:{kraken_balances['krk_base_currency_available']} \n(Quote) {config['krk_target_currency']} balance:{kraken_balances['krk_target_currency_available']}\n")
        #         #
        #         # # Binance: Get my balances
        #         binance_balances = get_binance_balances(bnb_exchange, config)
        #         # logger.info(f"Binance's Balances\n(Base) {config['bnb_base_currency']} balance:{binance_balances['bnb_base_currency_available']} \n(Quote) {config['bnb_target_currency']} balance:{binance_balances['bnb_target_currency_available']}\n")
        #
        #         # Log total balances
        #         total_base = round(float(kraken_balances['krk_base_currency_available']) + float(binance_balances['bnb_base_currency_available']), 8)
        #         total_quote = round(float(kraken_balances['krk_target_currency_available']) + float(binance_balances['bnb_target_currency_available']), 2)
        #         logger.info(f"Total balances: {config['bnb_base_currency']}={str(total_base)} | {config['bnb_target_currency']}={str(total_quote)}")
        #
        #         # # Check target currency price spreaderences in exchanges
        #         # # Crypto.com target currency ticker
        #         # cdc_tickers = await cdc_exchange.get_tickers()
        #         # cdc_ticker = cdc_tickers[cdc_pair]
        #         # cdc_buy_price = cdc_ticker.buy_price
        #         # cdc_sell_price = cdc_ticker.sell_price
        #         # # cdc_high = cdc_ticker.high
        #         # # cdc_low = cdc_ticker.low
        #         # logger.info(f'\nCRYPTO.COM => Market {cdc_pair.name}\nbuy price: {cdc_buy_price} - sell price: {cdc_sell_price}\n\n')
        #
        #         # for tpair in trading_pairs:
        #             # analyzed_pair = tpair
        #
        #         tpair = 'ADAUSDT'
        #         krk_balance = krk_exchange.query_private('Balance')
        #         krk_currency_available = 0.0
        #         if pair_coins[tpair]['base'] in krk_balance['result']:
        #             krk_currency_available = krk_balance['result'][pair_coins[tpair]['base']]
        #
        #         bnb_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[tpair]['quote'])
        #         if bnb_balance_result:
        #             bnb_currency_available = bnb_balance_result['free']
        #         else:
        #             bnb_currency_available = 0.0
        #
        #         # Kraken trading pair ticker
        #         krk_tickers = krk_exchange.query_public("Ticker", {'pair': tpair})['result'][tpair]
        #         krk_buy_price = krk_tickers['b'][0]
        #         krk_sell_price = krk_tickers['a'][0]
        #         # logger.info(f"\nKRAKEN => Market {tpair}\nbuy price: {krk_buy_price} - sell price: {krk_sell_price}\n")
        #
        #         # Binance trading pair ticker
        #         bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #         bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == tpair)
        #         bnb_buy_price = bnb_ticker['bidPrice']
        #         bnb_sell_price = bnb_ticker['askPrice']
        #         # logger.info(f"\nBINANCE => Market {config['bnb_trading_pair']}\nbuy price: {bnb_buy_price} - sell price: {bnb_sell_price}\n")
        #
        #         print(float(bnb_currency_available))
        #         print(float(krk_currency_available))
        #         print(float(config['trade_amount_bnb']) / float(krk_buy_price))
        #         # If balances not enough, raise exception
        #         if (float(bnb_currency_available) < config['trade_amount']) or (float(krk_currency_available) < (float(config['trade_amount_bnb']) / float(krk_buy_price))):
        #         # if float(kraken_balances['krk_target_currency_available']) < config['trade_amount'] or float(binance_balances['bnb_base_currency_available']) < (float(config['trade_amount_bnb']) / float(bnb_buy_price)):
        #             if config['telegram_notifications_on']:
        #                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{tpair}] Not enough funds to start! (trade amount {config['trade_amount']})")
        #             raise Exception(f"[{tpair}] Not enough funds to start! (trade amount {config['trade_amount']})")
        #
        #         #
        #         #     # Kraken trading pair ticker
        #         #     krk_tickers = krk_exchange.query_public("Ticker", {'pair': tpair})['result'][tpair]
        #         #     krk_buy_price = krk_tickers['b'][0]
        #         #     krk_sell_price = krk_tickers['a'][0]
        #         #     # logger.info(f"\nKRAKEN => Market {tpair}\nbuy price: {krk_buy_price} - sell price: {krk_sell_price}\n")
        #         #
        #         #     # Binance trading pair ticker
        #         #     bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #         #     bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == tpair)
        #         #     bnb_buy_price = bnb_ticker['bidPrice']
        #         #     bnb_sell_price = bnb_ticker['askPrice']
        #         #
        #         #     buy_prices = {'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #         #     # buy_prices = {'cdc': cdc_buy_price, 'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #         #     max_buy_price_key = max(buy_prices, key=buy_prices.get)
        #         #     max_buy_price = buy_prices[max_buy_price_key]
        #         #     sell_prices = {'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #         #     # sell_prices = {'cdc': cdc_sell_price, 'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #         #     min_sell_price_key = min(sell_prices, key=sell_prices.get)
        #         #     min_sell_price = sell_prices[min_sell_price_key]
        #         #     # logger.info(f"Max buy price -> {max_buy_price_key} = {max_buy_price}")
        #         #     # logger.info(f"Min sell price -> {min_sell_price_key} = {min_sell_price}")
        #         #     spread = round(float(max_buy_price) / float(min_sell_price), 8)
        #         #     logger.info(f"[{tpair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #         #
        #         #     item = {'pair': tpair, 'spread': spread, 'trading_pair_config_suffix': '', 'max_buy_price_key': max_buy_price_key, 'min_sell_price_key': min_sell_price_key}
        #         #     # Create list of potential opportunities
        #         #     # opportunity_list = {'spread': spread, 'trading_pair_config_suffix': '', 'max_buy_price_key': max_buy_price_key, 'min_sell_price_key': min_sell_price_key}
        #         #                         # {'spread': spread2, 'trading_pair_config_suffix': '2', 'max_buy_price_key': max_buy_price_key2, 'min_sell_price_key': min_sell_price_key2}]
        #         #     # Sort list by spread descending
        #         #     # sorted_opportunity_list = sorted(opportunity_list, key=lambda k: k['spread'], reverse=True)
        #         #
        #         #     # Prnt sorted_opportunity_list for reference
        #         #     # logger.info("Sorted Opportunity list:\n")
        #         #     # for item in sorted_opportunity_list:
        #         #     #     logger.info(f'{item}')
        #         #
        #         #     if item['spread'] > config['minimum_spread']:
        #         #     # if (item['spread'] > config['minimum_spread_krk'] and item['max_buy_price_key'] == 'krk') or (item['spread'] >= config['minimum_spread_bnb'] and item['max_buy_price_key'] == 'bnb'):
        #         #         opportunity_found = True
        #         #         break
        #         item = {'pair': 'ADAUSDT', 'spread': 1.00155, 'trading_pair_config_suffix': '', 'max_buy_price_key': 'krk', 'min_sell_price_key': 'bnb'}
        #         trend_6 = get_trend(3, krk_exchange, 'ADAUSDT', logger)
        #         trend_3 = get_trend(2, krk_exchange, 'ADAUSDT', logger)
        #         logger.info(f"Trend_6 = {trend_6}")
        #         logger.info(f"Trend_3 = {trend_3}")
        #         if abs(trend_6) >= 2 and abs(trend_3) > 0:
        #             opportunity_found = True
        #             # Kraken trading pair ticker
        #             krk_tickers = krk_exchange.query_public("Ticker", {'pair': 'ADAUSDT'})['result'][tpair]
        #             max_buy_price = krk_tickers['b'][0]
        #             krk_sell_price = krk_tickers['a'][0]
        #
        #         if opportunity_found and not config['safe_mode_on'] and trend_6 > 0:
        #             try:
        #                 # Set trading pair accordingly
        #                 # bnb_trading_pair = config['bnb_trading_pair' + item['trading_pair_config_suffix']]
        #                 # krk_trading_pair = config['krk_trading_pair' + item['trading_pair_config_suffix']]
        #
        #                 # if item['max_buy_price_key'] == 'krk' and item['min_sell_price_key'] == 'bnb':
        #                 if True:
        #                     ################################################################################################################################################
        #                     # Step 1:
        #                     # Limit orders (sell bnb_trading_pair in Binance and buy krk_trading_pair in Kraken, but first in Kraken since the exchange is slower)
        #                     ################################################################################################################################################
        #                     try:
        #                         quantity = str(round(float(config['trade_amount_bnb']) / float(max_buy_price), 1)) # ADA
        #                         # for _ in range(5):
        #                         #     try:
        #                         #         result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'market', 'oflags': 'fciq', 'volume': quantity})
        #                         #         logger.info(result_krk)
        #                         #         if result_krk['result']:
        #                         #             break
        #                         #     except:
        #                         #         logger.info(traceback.format_exc())
        #                         #         # wait a few seconds before trying again
        #                         #         continue
        #
        #                         # quantity = str(round(float(config['trade_amount_bnb']) / float(max_buy_price), 5)) #DOT
        #                         # orders_sent = 0
        #                         # for index, order_quantity in enumerate(sell_list):
        #                         #     quantity = str(round(float(order_quantity) / float(max_buy_price), 1))
        #                         #     price = str(float(max_buy_price) + ((index + 1)*0.000001))[0:str(float(max_buy_price) + ((index + 1)*0.000001)).find('.') + 7]
        #                         #
        #                         #     tries = 20
        #                         #     success = False
        #                         #     while tries >= 0 and not success:
        #                         #         try:
        #                         #             tries -= 1
        #                         #             result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': quantity})
        #                         #             logger.info(result_krk)
        #                         #             if result_krk['result']:
        #                         #                 trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread']})
        #                         #                 orders_sent += 1
        #                         #                 success = True
        #                         #         except:
        #                         #             logger.info(traceback.format_exc())
        #                         #             # wait a few seconds before trying again
        #                         #             continue
        #                         #
        #                         for _ in range(10):
        #                             try:
        #                                 # if trend_6 > 0:
        #                                 result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': str(round(float(max_buy_price) * 1.00155, 6))[0:str(round(float(max_buy_price) * 1.00155, 6)).find('.') + 7], 'volume': quantity}) #ADA
        #                                 # else:
        #                                     # result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': str(float(max_buy_price) + 0.000001)[0:str(float(max_buy_price) + 0.000001).find('.') + 7], 'volume': quantity}) #ADA
        #                                 if result_krk['result']:
        #                                     logger.info(result_krk)
        #                                     trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread']})
        #                                     break
        #                             except:
        #                                 logger.info(traceback.format_exc())
        #                                 continue
        #                         # result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': float(max_buy_price) + 0.0002, 'volume': quantity}) #DOT
        #                         if result_krk['error']:
        #                         # if orders_sent != len(sell_list):
        #                             # TODO: if some orders went through, revert them!
        #                             if config['telegram_notifications_on']:
        #                                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit orders in {item['min_sell_price_key']}")
        #                             raise Exception("Could not sell '{}' in pair '{}' for '{}' in Kraken: {}".format(config['trade_amount_bnb'], item['pair'], str(float(max_buy_price) + 0.000001), result_krk['error']))
        #                         quantity = str(round(float(config['trade_amount_bnb']) / float(max_buy_price), 1))
        #                         quantity_bnb = str(float(quantity) + (float(quantity) * config['bnb_fee']) + config['bnb_withdrawal_fee'])[0:str(float(quantity) + (float(quantity) * config['bnb_fee']) + config['bnb_withdrawal_fee']).find('.') + 2]
        #                         # result_bnb = bnb_exchange.order_limit_buy(symbol=item['pair'], quantity=quantity_bnb, price=str(float(min_sell_price) - 0.02)[0:str(float(min_sell_price) - 0.02).find('.') + 5]) #DOT
        #                         # result_bnb = bnb_exchange.order_limit_buy(symbol=item['pair'], quantity=quantity_bnb, price=str(float(min_sell_price) * 0.99899)[0:str(float(min_sell_price) * 0.99899).find('.') + 5]) #DOT with rate
        #                         for _ in range(10):
        #                             try:
        #                                 result_bnb = bnb_exchange.order_limit_buy(symbol=item['pair'], quantity=quantity_bnb, price=str(round(float(max_buy_price) - 0.0007, 5))) #ADA
        #                                 logger.info(result_bnb)
        #                                 trades.append({'exchange': 'bnb', 'orderid': result_bnb['orderId'], 'time': time.time(), 'spread': item['spread']})
        #                                 break
        #                             except:
        #                                 logger.info(traceback.format_exc())
        #                                 continue
        #
        #                         # TODO: sometimes, trades are done but the API throws an exception, causing the script to go to the except part
        #                         # we can fix this by checking if a trade was done at all...
        #                         if not result_bnb:
        #                             if config['telegram_notifications_on']:
        #                                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['max_buy_price_key']}")
        #                             raise Exception("Could not sell '{}' in pair '{}' for '{}' in Binance.".format(config['trade_amount_bnb'], config['bnb_trading_pair'], str(float(max_buy_price))))
        #                     except:
        #                         if config['telegram_notifications_on']:
        #                             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit orders.")
        #                         logger.info(traceback.format_exc())
        #                         raise Exception("Could not sell '{}' in pair '{}'.".format(config['trade_amount_bnb'], item['pair']))
        #
        #                     # Wait 10 seconds to give exchanges time to process orders
        #                     # logger.info('Waiting 10 seconds to give exchanges time to process orders...')
        #                     if config['telegram_notifications_on']:
        #                         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): limit orders sent and waiting for fulfillment...")
        #                     # await asyncio.sleep(10)
        #
        #                     ################################################################################################################################################
        #                     # Step 2:
        #                     # Wait until limit orders have been fulfilled
        #                     ################################################################################################################################################
        #                     limit_orders_closed = await wait_for_orders(trades, config, krk_exchange, bnb_exchange, item['pair'], logger)
        #                     if not limit_orders_closed:
        #                         if config['telegram_notifications_on']:
        #                             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): At least 1 limit order could not be fulfilled after {config['limit_order_time']} seconds. You must find a recover way!")
        #                         raise Exception("Limit orders not fulfilled!")
        #
        #                         # tries = 10
        #                         # success = False
        #                         # krk_open_orders = []
        #                         # bnb_open_orders = []
        #                         # while tries >= 0 and not success:
        #                         #     try:
        #                         #         tries = tries - 1
        #                         #         krk_open_orders = krk_exchange.query_private('OpenOrders')['result']['open']
        #                         #         bnb_open_orders = bnb_exchange.get_open_orders()
        #                         #         logger.info(f'Kraken open trades: {krk_open_orders}')
        #                         #         logger.info(f'Binance open trades: {bnb_open_orders}')
        #                         #         success = True
        #                         #     except:
        #                         #         logger.info(traceback.format_exc())
        #                         #         # wait a few seconds before trying again
        #                         #         await asyncio.sleep(1)
        #                         #         continue
        #                         # # If Kraken orders not fulfilled
        #                         # if krk_open_orders:
        #                         #     # Cancel all open orders
        #                         #     for _ in range(50):
        #                         #         try:
        #                         #             krk_result = krk_exchange.query_private('CancelAll')
        #                         #             break
        #                         #         except:
        #                         #             logger.info(traceback.format_exc())
        #                         #             await asyncio.sleep(1)
        #                         #             continue
        #                         #      # Create new one and try to fulfill it fast
        #                         #     tries = 100
        #                         #     success = False
        #                         #     trades = []
        #                         #     first_try = True
        #                         #     while tries >= 0 and not success:
        #                         #         try:
        #                         #             logger.info("Trying to limit sell in Kraken...")
        #                         #             tries -= 1
        #                         #
        #                         #             # calculate trade_amount left
        #                         #             krk_balance = krk_exchange.query_private('Balance')
        #                         #             krk_currency_available = 0.0
        #                         #             if pair_coins[item['pair']]['quote'] in krk_balance['result']:
        #                         #                 krk_currency_available = krk_balance['result'][pair_coins[item['pair']]['quote']]
        #                         #             trade_amount_left = float(config['trade_amount']) - (float(krk_currency_available) - float(kraken_balances['krk_target_currency_available']))
        #                         #             logger.info(f"Trade Amount left -> {str(trade_amount_left)}")
        #                         #             krk_tickers = krk_exchange.query_public("Ticker", {'pair': item['pair']})['result'][item['pair']]
        #                         #             krk_buy_price = krk_tickers['b'][0]
        #                         #             # quantity = str(round(trade_amount_left / float(krk_buy_price), 1))
        #                         #             ada_already_bought = round(float(krk_currency_available) / float(max_buy_price), 1)
        #                         #             quantity = round(float(quantity_bnb) - ada_already_bought - 0.5, 1)
        #                         #
        #                         #
        #                         #
        #                         #             if float(trade_amount_left) < 1.0 and not first_try:
        #                         #                 success = True
        #                         #                 trades = []
        #                         #             else:
        #                         #                 result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': str(float(krk_buy_price) + 0.000003)[0:str(float(krk_buy_price) + 0.000003).find('.') + 7], 'volume': quantity})
        #                         #                 logger.info(result_krk)
        #                         #                 if result_krk['result']:
        #                         #                     first_try = False
        #                         #                 trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': item['pair']})
        #                         #                 limit_order_successful = await short_wait_for_krk_order(trades, config, krk_exchange, logger)
        #                         #                 success = limit_order_successful
        #                         #                 trades = []
        #                         #         except:
        #                         #             logger.info(traceback.format_exc())
        #                         #             # wait a few seconds before trying again
        #                         #             await asyncio.sleep(2)
        #                         #             continue
        #                         #
        #                         #     if not success:
        #                         #         if config['telegram_notifications_on']:
        #                         #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [USDTEUR] Error when selling in Kraken")
        #                         #             # telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair_coins[candidates[0]['pair']]['bnb_base']}] Error when selling in Kraken")
        #                         #         raise Exception(f"[USDTEUR] Error when selling in Kraken")
        #                         #         # raise Exception(f"[{pair_coins[candidates[0]['pair']]['bnb_base']}] Error when selling in Kraken")
        #                         # else: #binance order got stuck
        #                             # # Cancel open orders
        #                             # for _ in range (10):
        #                             #     try:
        #                             #         result_bnb = bnb_exchange.cancel_order(symbol=item['pair'], orderId=trades[0]['orderid'])
        #                             #         logger.info(result_bnb)
        #                             #         trades = []
        #                             #         break
        #                             #     except:
        #                             #         logger.info(traceback.format_exc())
        #                             #         # wait a few seconds before trying again
        #                             #         await asyncio.sleep(2)
        #                             #         continue
        #                             #
        #                             # # Re-try trade until success
        #                             # tries = 100
        #                             # success = False
        #                             # trades = []
        #                             # first_try = True
        #                             # while tries >= 0 and not success:
        #                             #     try:
        #                             #         logger.info("Trying to limit sell in Binance...")
        #                             #         tries -= 1
        #                             #
        #                             #         # calculate trade_amount left
        #                             #         bnb_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[item['pair']]['base'])
        #                             #         if bnb_balance_result:
        #                             #             bnb_base_currency_available = bnb_balance_result['free']
        #                             #         else:
        #                             #             bnb_base_currency_available = 0.0
        #                             #
        #                             #         # calculate quote amount spent
        #                             #         bnb_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[item['pair']]['quote'])
        #                             #         if bnb_balance_result:
        #                             #             bnb_quote_currency_available = bnb_balance_result['free']
        #                             #         else:
        #                             #             bnb_quote_currency_available = 0.0
        #                             #         usdt_already_spent = round(float(binance_balances['bnb_target_currency_available']) - float(bnb_quote_currency_available), 2)
        #                             #
        #                             #         bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #                             #         bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == tpair)
        #                             #         bnb_sell_price = bnb_ticker['askPrice']
        #                             #
        #                             #         quantity = str(round((float(config['trade_amount_bnb']) / float(max_buy_price)) - float(bnb_base_currency_available), 1))
        #                             #         logger.info(f"ADA Quantity Amount left -> {str(quantity)}")
        #                             #         quantity_bnb = str(float(quantity) + (float(quantity) * config['bnb_fee']) + config['bnb_withdrawal_fee'])[0:str(float(quantity) + (float(quantity) * config['bnb_fee']) + config['bnb_withdrawal_fee']).find('.') + 2]
        #                             #         # if usdt_already_spent >= (float(config['trade_amount']) - 60) and not first_try:
        #                             #         if float(quantity) <= 25.0 and not first_try:
        #                             #             success = True
        #                             #             trades = []
        #                             #         else:
        #                             #             result_bnb = bnb_exchange.order_limit_buy(symbol=item['pair'], quantity=quantity_bnb, price=str(round(float(bnb_sell_price) - 0.0009, 5)))
        #                             #             logger.info(result_bnb)
        #                             #             trades.append({'exchange': 'bnb', 'orderid': result_bnb['orderId'], 'time': time.time(), 'spread': item['spread']})
        #                             #             logger.info(result_krk)
        #                             #             if result_bnb:
        #                             #                 first_try = False
        #                             #             limit_order_successful = await short_wait_for_bnb_order(trades, config, bnb_exchange, logger)
        #                             #             success = limit_order_successful
        #                             #             trades = []
        #                             #     except:
        #                             #         logger.info(traceback.format_exc())
        #                             #         # wait a few seconds before trying again
        #                             #         await asyncio.sleep(2)
        #                             #         continue
        #                             #
        #                             # if not success:
        #                             #     if config['telegram_notifications_on']:
        #                             #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [USDTEUR] Error when buying in Binance")
        #                             #         # telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair_coins[candidates[0]['pair']]['bnb_base']}] Error when selling in Kraken")
        #                             #     raise Exception(f"[USDTEUR] Error when buying in Binance")
        #                             #
        #                             # # Create orders quickly until fulfillment
        #
        #                             # limit_orders_closed = await wait_for_orders(trades, config, krk_exchange, bnb_exchange, item['pair'], logger)
        #                             # if not limit_orders_closed:
        #                             #     if config['telegram_notifications_on']:
        #                             #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): Order in bnb not fulfilled after {config['limit_order_time']} seconds")
        #
        #
        #                         # raise Exception("At least 1 limit order could not be fulfilled")
        #
        #
        #
        #
        #                     # trades = []
        #
        #                     await asyncio.sleep(10)
        #                     ################################################################################################################################################
        #                     # Step 3:
        #                     # Send bought USDT from Kraken to Binance
        #                     ################################################################################################################################################
        #                     tries = 100
        #                     success = False
        #                     krk_withdraw_refid = ''
        #                     while tries >= 0 and not success:
        #                         try:
        #                             tries -= 1
        #
        #                             krk_balance = krk_exchange.query_private('Balance')
        #                             krk_base_currency_available = 0.0
        #                             if 'USDT' in krk_balance['result']:
        #                                 krk_base_currency_available = krk_balance['result']['USDT']
        #                                 # krk_base_currency_available = krk_balance['result'][pair_coins[candidates[0]['pair']]['base']]
        #
        #                             withdrawal_result_krk = krk_exchange.query_private('Withdraw', {'asset': pair_coins[item['pair']]['quote'], 'key': pair_coins[item['pair']]['krk_bnb_address'], 'amount': krk_base_currency_available})
        #                             if withdrawal_result_krk['result']:
        #                                 logger.info(f"Withdraw info from Kraken -> {withdrawal_result_krk['result']}")
        #                                 krk_withdraw_refid = withdrawal_result_krk['result']['refid']
        #                                 success = True
        #                                 # Withdrawal id (in order to check status of withdrawal later)
        #                         except:
        #                             logger.info(traceback.format_exc())
        #                             # wait a few seconds before trying again
        #                             await asyncio.sleep(5)
        #                             continue
        #
        #                     ################################################################################################################################################
        #                     # Step 4:
        #                     # Send fast coin from Binance to Kraken
        #                     ################################################################################################################################################
        #                     # Get fast coin balance in Binance
        #                     # TODO: Wait until the trade was completed and there are funds to withdraw
        #                     tries = 100
        #                     success = False
        #                     bnb_withdraw_id = ''
        #                     withdrawal_result_bnb = None
        #                     while tries >= 0 and not success:
        #                         try:
        #                             info = bnb_exchange.get_symbol_info(item['pair'])
        #                             logger.info("Trying to withdraw 2nd coin from Binance...")
        #                             tries -= 1
        #                             bnb_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[item['pair']]['base'])
        #                             if bnb_balance_result:
        #                                 bnb_base_currency_available = bnb_balance_result['free'][0:bnb_balance_result['free'].find('.') + info['filters'][2]['tickSize'].find('1')]
        #                             else:
        #                                 bnb_base_currency_available = 0.0
        #                             withdrawal_result_bnb = bnb_exchange.withdraw(asset=pair_coins[item['pair']]['base'], address=pair_coins[item['pair']]['bnb_krk_address'], amount=quantity_bnb)
        #                             # withdrawal_result_bnb = bnb_exchange.withdraw(asset=pair_coins[item['pair']]['base'], address=pair_coins[item['pair']]['bnb_krk_address'], amount=bnb_base_currency_available)
        #
        #                             if withdrawal_result_bnb['success']:
        #                                 logger.info(withdrawal_result_bnb)
        #                                 bnb_withdraw_id = withdrawal_result_bnb['id']
        #                                 success = True
        #                         except:
        #                             logger.info(traceback.format_exc())
        #                             # wait a few seconds before trying again
        #                             await asyncio.sleep(10)
        #                             continue
        #
        #                     if not success:
        #                         if config['telegram_notifications_on']:
        #                             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair_coins[item['pair']]['base']}] Error when withdrawing from Binance")
        #                         logger.info(traceback.format_exc())
        #                         raise Exception(f"[{pair_coins[item['pair']]['base']}] Error when withdrawing from Binance")
        #
        #                     ################################################################################################################################################
        #                     # Step 6:
        #                     # Wait for withdrawals
        #                     ################################################################################################################################################
        #                     withdrawals_processed = await wait_for_withdrawals(krk_withdraw_refid, bnb_withdraw_id, config, krk_exchange, bnb_exchange, pair_coins[item['pair']]['quote'], pair_coins[item['pair']]['base'], logger)
        #                     # withdrawals_processed = await wait_for_withdrawals(krk_withdraw_refid, bnb_withdraw_id, config, krk_exchange, bnb_exchange, pair_coins[item['pair']]['quote'], pair_coins[item['pair']]['base'], logger)
        #                     if not withdrawals_processed:
        #                         logger.info(f"Waited too long for Withdrawals/deposits\n")
        #                         if config['telegram_notifications_on']:
        #                             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))}: waited too long for Withdrawals/Deposits!")
        #                     else:
        #                         logger.info(f"Withdrawals/deposits completed\n")
        #                         if config['telegram_notifications_on']:
        #                             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Opportunity of {str(round(float(item['spread']), 5))}: Withdrawals/Deposits completed")
        #
        #
        #                 # elif item['max_buy_price_key'] == 'bnb' and item['min_sell_price_key'] == 'krk':
        #                 #     ################################################################################################################################################
        #                 #     # Step 1:
        #                 #     # Limit orders (sell bnb_trading_pair in Binance and buy krk_trading_pair in Kraken, but first in Kraken since the exchange is slower)
        #                 #     ################################################################################################################################################
        #                 #     try:
        #                 #         quantity = str(round(float(config['trade_amount_bnb']) / float(max_buy_price), 1))
        #                 #         # result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'buy', 'ordertype': 'market', 'oflags': 'fciq', 'volume': quantity})
        #                 #         result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'buy', 'ordertype': 'limit', 'oflags': 'fciq', 'price': min_sell_price, 'volume': quantity})
        #                 #         if result_krk['error']:
        #                 #             if config['telegram_notifications_on']:
        #                 #                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['min_sell_price_key']}")
        #                 #             raise Exception("Could not sell '{}' in pair '{}' for '{}' in Kraken: {}".format(config['trade_amount_bnb'], item['pair'], str(round(float(min_sell_price) - 0.0001, 5)), result_krk['error']))
        #                 #         result_bnb = bnb_exchange.order_limit_sell(symbol=config['bnb_trading_pair'], quantity=quantity, price=str(round(float(max_buy_price) + 0.0008, 5)))
        #                 #         logger.info(result_krk)
        #                 #         logger.info(result_bnb)
        #                 #         trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread']})
        #                 #         trades.append({'exchange': 'bnb', 'orderid': result_bnb['orderId'], 'time': time.time(), 'spread': item['spread']})
        #                 #
        #                 #         if not result_bnb:
        #                 #             if config['telegram_notifications_on']:
        #                 #                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['max_buy_price_key']}")
        #                 #             raise Exception("Could not sell '{}' in pair '{}' for '{}' in Binance.".format(config['trade_amount_bnb'], config['bnb_trading_pair'], str(float(max_buy_price))))
        #                 #         # TODO:
        #                 #         # sometimes kraken returns an error but the trade was made so we need to find a proper solution for this
        #                 #         # if result_krk['error']:
        #                 #         #     if config['telegram_notifications_on']:
        #                 #         #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['min_sell_price_key']}")
        #                 #         #         raise Exception("Could not buy '{}' in pair '{}' for '{}' in Kraken: {}".format(config['trade_amount_bnb'], item['pair'], trade_price, result_krk['error']))
        #                 #     except:
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit orders.")
        #                 #         logger.info(traceback.format_exc())
        #                 #         raise Exception("Could not sell '{}' in pair '{}' in Binance.".format(config['trade_amount_bnb'], config['bnb_trading_pair']))
        #                 #
        #                 #     # Wait 10 seconds to give exchanges time to process orders
        #                 #     logger.info('Waiting 10 seconds to give exchanges time to process orders...')
        #                 #     if config['telegram_notifications_on']:
        #                 #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): limit orders sent and waiting for fulfillment...")
        #                 #     await asyncio.sleep(10)
        #                 #
        #                 #     ################################################################################################################################################
        #                 #     # Step 2:
        #                 #     # Wait until limit orders have been fulfilled
        #                 #     ################################################################################################################################################
        #                 #     limit_orders_closed = await wait_for_orders(trades, config, krk_exchange, bnb_exchange, item['pair'], logger)
        #                 #     if not limit_orders_closed:
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ): At least 1 limit order could not be fulfilled after {config['limit_order_time']} seconds")
        #                 #         raise Exception("At least 1 limit order could not be fulfilled")
        #                 #     trades = []
        #                 #
        #                 #     await asyncio.sleep(20)
        #                 #     ################################################################################################################################################
        #                 #     # Step 3:
        #                 #     # Send bought crypto from Kraken to Binance
        #                 #     ################################################################################################################################################
        #                 #     tries = 100
        #                 #     success = False
        #                 #     krk_withdraw_refid = ''
        #                 #     while tries >= 0 and not success:
        #                 #         try:
        #                 #             tries -= 1
        #                 #             kraken_balances_2 = get_kraken_balances(krk_exchange, config)
        #                 #             withdrawal_result_krk = krk_exchange.query_private('Withdraw', {'asset': config['krk_base_currency'], 'key': config['krk_bnb_xrp_address_key'], 'amount': kraken_balances_2['krk_base_currency_available']})
        #                 #             if withdrawal_result_krk['result']:
        #                 #                 logger.info(f"Withdraw info from Kraken -> {withdrawal_result_krk['result']}")
        #                 #                 krk_withdraw_refid = withdrawal_result_krk['result']['refid']
        #                 #                 success = True
        #                 #                 # Withdrawal id (in order to check status of withdrawal later)
        #                 #         except:
        #                 #             logger.info(traceback.format_exc())
        #                 #             # wait a few seconds before trying again
        #                 #             await asyncio.sleep(5)
        #                 #             continue
        #                 #     ################################################################################################################################################
        #                 #     # Step 4:
        #                 #     # Trade fiat for a fast crypto currency in order to send it in Binance
        #                 #     ################################################################################################################################################
        #                 #     # First check which fast coin is better today
        #                 #     candidates = []
        #                 #     # for _ in range(50):
        #                 #     #     candidates = []
        #                 #     #     try:
        #                 #     #         candidates = get_candidates(krk_exchange, ticker_pairs)
        #                 #     #         print(f"Candidates -> {candidates}")
        #                 #     #         break
        #                 #     #     except:
        #                 #     #         logger.info(traceback.format_exc())
        #                 #     #         await asyncio.sleep(10)
        #                 #     #         continue
        #                 #     #
        #                 #     # if candidates:
        #                 #     #     candidate = {'pair': pair_coins[candidates[0]['pair']]['bnb_pair'], 'spread': candidates[0]['spread']}
        #                 #     # else:
        #                 #     #     candidate = {'pair': 'DOTEUR', 'spread': 1.0} # let's choose DOT as it's the one with highest market cap for now
        #                 #     #     candidates.append(candidate)
        #                 #
        #                 #     # candidate = {'pair': 'DOTEUR', 'spread': 1.0} # let's choose DOT as it's the one with highest market cap for now
        #                 #     # candidates.append(candidate)
        #                 #
        #                 #     # USDT mode
        #                 #     candidate = {'pair': 'EURUSDT', 'spread': 1.0}
        #                 #     candidates.append(candidate)
        #                 #
        #                 #     # tries = 90
        #                 #     # success = False
        #                 #     # result_bnb = None
        #                 #     # trades = []
        #                 #     # first_try = True
        #                 #     # while tries >= 0 and not success:
        #                 #     #     tries -= 1
        #                 #     #     try:
        #                 #     #         # Calculate difference of fiat to be used in the next order
        #                 #     #         tries_inside = 100
        #                 #     #         success_inside = False
        #                 #     #         while tries_inside >= 0 and not success_inside:
        #                 #     #             try:
        #                 #     #                 tries_inside -= 1
        #                 #     #                 bnb_fiat_balance_result = bnb_exchange.get_asset_balance(asset=config['bnb_target_currency'])
        #                 #     #                 if bnb_fiat_balance_result:
        #                 #     #                     bnb_fiat_currency_available = bnb_fiat_balance_result['free']
        #                 #     #                 else:
        #                 #     #                     bnb_fiat_currency_available = "0.0"
        #                 #     #                 # bnb_candidate_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[candidate['pair']]['base'])
        #                 #     #                 # if bnb_candidate_balance_result:
        #                 #     #                 #     bnb_base_currency_available = float(bnb_candidate_balance_result['free'])
        #                 #     #                 # else:
        #                 #     #                 #     bnb_base_currency_available = 0.0
        #                 #     #                 success_inside = True
        #                 #     #             except:
        #                 #     #                 logger.info(traceback.format_exc())
        #                 #     #                 # wait a few seconds before trying again
        #                 #     #                 await asyncio.sleep(5)
        #                 #     #                 continue
        #                 #     #
        #                 #     #         fiat_amount = round(float(bnb_fiat_currency_available) - 0.1, 1)
        #                 #     #         logger.info("Trying 2nd limit order...")
        #                 #     #         # Get pair info
        #                 #     #         info = bnb_exchange.get_symbol_info(candidate['pair'])
        #                 #     #         # Binance trading pair ticker
        #                 #     #         bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #                 #     #         bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == candidate['pair'])
        #                 #     #         bnb_buy_price = bnb_ticker['bidPrice']
        #                 #     #         # quantity = round((fiat_amount / float(bnb_buy_price) - 0.005), info['filters'][2]['stepSize'].find('1') - 1) # get decimals from API
        #                 #     #         # quantity = round(fiat_amount / (float(bnb_buy_price) * 0.999995), info['filters'][2]['stepSize'].find('1') - 1) # get decimals from API
        #                 #     #         quantity = bnb_fiat_currency_available[0:bnb_fiat_currency_available.find('.') + info['filters'][2]['stepSize'].find('1')] # get decimals from API
        #                 #     #         price = str(float(bnb_buy_price) + 0.0002)[0:str(float(bnb_buy_price) + 0.0002).find('.') + info['filters'][0]['tickSize'].find('1')]
        #                 #     #         result_bnb = bnb_exchange.order_limit_sell(symbol=candidate['pair'], quantity=quantity, price=price)
        #                 #     #         # result_bnb = bnb_exchange.order_limit_buy(symbol=candidate['pair'], quantity=quantity, price=str(round(float(bnb_buy_price) * 0.999995, info['filters'][0]['tickSize'].find('1') - 1)))
        #                 #     #         # result_bnb = bnb_exchange.order_market_buy(symbol=candidate['pair'], quantity=quantity)
        #                 #     #         logger.info(result_bnb)
        #                 #     #         first_try = False
        #                 #     #         trades.append({'exchange': 'bnb', 'orderid': result_bnb['orderId'], 'time': time.time(), 'spread': item['spread'], 'pair': candidate['pair']})
        #                 #     #         limit_order_successful = await short_wait_for_bnb_order(trades, config, bnb_exchange, logger)
        #                 #     #         # Calculate difference of fiat to be used in the next order
        #                 #     #         tries_inside = 100
        #                 #     #         success_inside = False
        #                 #     #         while tries_inside >= 0 and not success_inside:
        #                 #     #             try:
        #                 #     #                 tries_inside -= 1
        #                 #     #                 bnb_fiat_balance_result = bnb_exchange.get_asset_balance(asset=config['bnb_target_currency'])
        #                 #     #                 if bnb_fiat_balance_result:
        #                 #     #                     bnb_fiat_currency_available = bnb_fiat_balance_result['free']
        #                 #     #                 else:
        #                 #     #                     bnb_fiat_currency_available = "0.0"
        #                 #     #                 # bnb_candidate_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[candidate['pair']]['base'])
        #                 #     #                 # if bnb_candidate_balance_result:
        #                 #     #                 #     bnb_base_currency_available = float(bnb_candidate_balance_result['free'])
        #                 #     #                 # else:
        #                 #     #                 #     bnb_base_currency_available = 0.0
        #                 #     #                 success_inside = True
        #                 #     #             except:
        #                 #     #                 logger.info(traceback.format_exc())
        #                 #     #                 # wait a few seconds before trying again
        #                 #     #                 await asyncio.sleep(5)
        #                 #     #                 continue
        #                 #     #         if limit_order_successful and float(bnb_fiat_currency_available) < 10.0:
        #                 #     #             success = True
        #                 #     #             trades = []
        #                 #     #     except:
        #                 #     #         logger.info(traceback.format_exc())
        #                 #     #         # wait a few seconds before trying again
        #                 #     #         await asyncio.sleep(10)
        #                 #     #         continue
        #                 #     # if not success:
        #                 #     #     if config['telegram_notifications_on']:
        #                 #     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{candidate['pair']}]. Error when placing limit buy order in Binance")
        #                 #     #     logger.info(traceback.format_exc())
        #                 #     #     raise Exception("Could not limit sell '{}' for '{}' in pair '{}' in Binance.".format(quantity, price, candidate['pair']))
        #                 #     # else:
        #                 #     #     if config['telegram_notifications_on']:
        #                 #     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{candidate['pair']}] 2nd limit order successful")
        #                 #     #     logger.info(f"[{candidate['pair']}] 2nd limit order successful")
        #                 #     #
        #                 #     # await asyncio.sleep(20)
        #                 #
        #                 #     ################################################################################################################################################
        #                 #     # Step 5:
        #                 #     # Send fast coin from Binance to Kraken
        #                 #     ################################################################################################################################################
        #                 #     # Get fast coin balance in Binance
        #                 #     # TODO: Wait until the trade was completed and there are funds to withdraw
        #                 #     tries = 100
        #                 #     success = False
        #                 #     bnb_withdraw_id = ''
        #                 #     withdrawal_result_bnb = None
        #                 #     while tries >= 0 and not success:
        #                 #         try:
        #                 #             info = bnb_exchange.get_symbol_info(candidate['pair'])
        #                 #             logger.info("Trying to withdraw 2nd pair from Binance...")
        #                 #             tries -= 1
        #                 #             bnb_balance_result = bnb_exchange.get_asset_balance(asset=pair_coins[candidates[0]['pair']]['quote'])
        #                 #             if bnb_balance_result:
        #                 #                 bnb_base_currency_available = bnb_balance_result['free'][0:bnb_balance_result['free'].find('.') + info['filters'][2]['stepSize'].find('1')]
        #                 #             else:
        #                 #                 bnb_base_currency_available = 0.0
        #                 #             withdrawal_result_bnb = bnb_exchange.withdraw(asset=pair_coins[candidates[0]['pair']]['quote'], address=pair_coins[candidates[0]['pair']]['krk_address'], amount=bnb_base_currency_available)
        #                 #
        #                 #             if withdrawal_result_bnb['success']:
        #                 #                 logger.info(withdrawal_result_bnb)
        #                 #                 bnb_withdraw_id = withdrawal_result_bnb['id']
        #                 #                 success = True
        #                 #         except:
        #                 #             logger.info(traceback.format_exc())
        #                 #             # wait a few seconds before trying again
        #                 #             await asyncio.sleep(10)
        #                 #             continue
        #                 #
        #                 #     if not success:
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair_coins[candidates[0]['pair']]['quote']}] Error when withdrawing from Binance")
        #                 #         logger.info(traceback.format_exc())
        #                 #         raise Exception(f"[{pair_coins[candidates[0]['pair']]['quote']}] Error when withdrawing from Binance")
        #                 #
        #                 #     ################################################################################################################################################
        #                 #     # Step 6:
        #                 #     # Wait for withdrawals
        #                 #     ################################################################################################################################################
        #                 #     withdrawals_processed = await wait_for_withdrawals(krk_withdraw_refid, bnb_withdraw_id, config, krk_exchange, bnb_exchange, pair_coins[item['pair']]['quote'], pair_coins[item['pair']]['base'], logger)
        #                 #     # withdrawals_processed = await wait_for_withdrawals(krk_withdraw_refid, bnb_withdraw_id, config, krk_exchange, bnb_exchange, pair_coins[item['pair']]['quote'], pair_coins[item['pair']]['base'], logger)
        #                 #     if not withdrawals_processed:
        #                 #         logger.info(f"Waited too long for Withdrawals/deposits\n")
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))}: waited too long for Withdrawals/Deposits!")
        #                 #     else:
        #                 #         logger.info(f"Withdrawals/deposits completed\n")
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))}: Withdrawals/Deposits completed")
        #                 #
        #                 #     ################################################################################################################################################
        #                 #     # Step 7:
        #                 #     # Limit sell stable coin for fiat in Kraken
        #                 #     ################################################################################################################################################
        #                 #     # Check 60 minute candel to try to set the best sell price
        #                 #     # trend = 0
        #                 #     # try:
        #                 #     #     for _ in range(3):
        #                 #     #         ohlc_id = int(krk_exchange.query_public('OHLC', {'pair': candidates[0]['pair'], 'interval': '1'})['result']['last']) - 1
        #                 #     #         ohlc = krk_exchange.query_public('OHLC', {'pair': candidates[0]['pair'], 'interval': '1', 'since': ohlc_id})
        #                 #     #         # ohlc_o1 = ohlc['result'][pair][0][1]
        #                 #     #         # ohlc_c1 = ohlc['result'][pair][0][4]
        #                 #     #         if ohlc['result'][candidates[0]['pair']][0][4] > ohlc['result'][candidates[0]['pair']][0][1]:
        #                 #     #             trend += 1
        #                 #     #         elif ohlc['result'][candidates[0]['pair']][0][4] < ohlc['result'][candidates[0]['pair']][0][1]:
        #                 #     #             trend -= 1
        #                 #     # except:
        #                 #     #     raise("Could not calculate trend in last step")
        #                 #
        #                 #     # tries = 90
        #                 #     # success = False
        #                 #     # trades = []
        #                 #     # first_try = True
        #                 #     # while tries >= 0 and not success:
        #                 #     #     try:
        #                 #     #         logger.info("Trying to limit sell in Kraken...")
        #                 #     #         tries -= 1
        #                 #     #         # Get decimals info
        #                 #     #         krk_info = krk_exchange.query_public('AssetPairs', {'pair': 'USDTEUR'})
        #                 #     #         decimals = krk_info['result']['USDTEUR']['pair_decimals']
        #                 #     #         # krk_info = krk_exchange.query_public('AssetPairs', {'pair': candidates[0]['pair']})
        #                 #     #         # decimals = krk_info['result'][candidates[0]['pair']]['pair_decimals']
        #                 #     #
        #                 #     #         krk_balance = krk_exchange.query_private('Balance')
        #                 #     #         krk_base_currency_available = 0.0
        #                 #     #         # if pair_coins[candidates[0]['pair']]['base'] in krk_balance['result']:
        #                 #     #         if 'USDT' in krk_balance['result']:
        #                 #     #             krk_base_currency_available = krk_balance['result']['USDT']
        #                 #     #             # krk_base_currency_available = krk_balance['result'][pair_coins[candidates[0]['pair']]['base']]
        #                 #     #         if float(krk_base_currency_available) < 5.0 and not first_try:
        #                 #     #             success = True
        #                 #     #             trades = []
        #                 #     #         else:
        #                 #     #             krk_tickers = krk_exchange.query_public("Ticker", {'pair': 'USDTEUR'})['result']['USDTEUR']
        #                 #     #             # krk_tickers = krk_exchange.query_public("Ticker", {'pair': candidates[0]['pair']})['result'][candidates[0]['pair']]
        #                 #     #             krk_buy_price = krk_tickers['b'][0]
        #                 #     #             price = (float(krk_buy_price) + 0.0001)
        #                 #     #             # price = round(float(krk_buy_price) * 1.0004, decimals)
        #                 #     #             # krk_sell_price = krk_tickers['a'][0]
        #                 #     #             # if trend >= 0:
        #                 #     #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                 #     #             # else:
        #                 #     #             #     price = round(float(bnb_buy_price) * 0.99982, 5)
        #                 #     #             # if float(bnb_buy_price) > float(krk_sell_price):
        #                 #     #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                 #     #             # else:
        #                 #     #             #     price = round(float(krk_sell_price) * 1.0005, 5)
        #                 #     #             result_krk = krk_exchange.query_private('AddOrder', {'pair': 'USDTEUR', 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                 #     #             # result_krk = krk_exchange.query_private('AddOrder', {'pair': candidates[0]['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                 #     #             logger.info(result_krk)
        #                 #     #             if result_krk['result']:
        #                 #     #                 first_try = False
        #                 #     #             trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': 'USDTEUR'})
        #                 #     #             # trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': candidate['pair']})
        #                 #     #             limit_order_successful = await short_wait_for_krk_order(trades, config, krk_exchange, logger)
        #                 #     #             success = limit_order_successful
        #                 #     #             trades = []
        #                 #     #             # if result_krk['error']:
        #                 #     #             #     if config['telegram_notifications_on']:
        #                 #     #             #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{candidates[0]['pair']}] Error when placing limit order in Kraken ({krk_base_currency_available} @ {price})")
        #                 #     #             #     raise Exception(f"Error when placing sell limit order in Kraken ({krk_base_currency_available} {candidates[0]['pair']} @ {price})")
        #                 #     #
        #                 #     #             # if result_krk['error']:
        #                 #     #             #     if config['telegram_notifications_on']:
        #                 #     #             #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{candidates[0]['pair']}] Error when placing limit order in Kraken ({krk_base_currency_available} @ {price})")
        #                 #     #             #     raise Exception(f"Error when placing sell limit order in Kraken ({krk_base_currency_available} {candidates[0]['pair']} @ {price})")
        #                 #     #     except:
        #                 #     #         logger.info(traceback.format_exc())
        #                 #     #         # wait a few seconds before trying again
        #                 #     #         await asyncio.sleep(10)
        #                 #     #         continue
        #                 #     #
        #                 #     # if not success:
        #                 #     #     if config['telegram_notifications_on']:
        #                 #     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [USDTEUR] Error when selling in Kraken")
        #                 #     #         # telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair_coins[candidates[0]['pair']]['bnb_base']}] Error when selling in Kraken")
        #                 #     #     raise Exception(f"[USDTEUR] Error when selling in Kraken")
        #                 #     #     # raise Exception(f"[{pair_coins[candidates[0]['pair']]['bnb_base']}] Error when selling in Kraken")
        #                 #
        #                 # # elif item['max_buy_price_key'] == 'krk' and item['min_sell_price_key'] == 'bnb':
        #                 # #     # Make orders only if there are enough funds in both exchanges to perform both trades
        #                 # #     # TODO:
        #                 # #
        #                 # #     # Limit order to sell pair in Kraken
        #                 # #     # First reduce the price a bit if the spread is greater than maximum_spread_krk (in order to make sure the trade will be fulfilled in Kraken)
        #                 # #     if config['limit_spread_krk'] and item['spread'] > config['maximum_spread_krk']:
        #                 # #         trade_price = str(round(float(min_sell_price) * item['spread'] * config['reduction_rate_krk'], 1))
        #                 # #     else:
        #                 # #         trade_price = str(round(float(max_buy_price), 1))
        #                 # #
        #                 # #     result = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': trade_price, 'volume': config['trade_amount_krk']})
        #                 # #     if result['error']:
        #                 # #         if config['telegram_notifications_on']:
        #                 # #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['max_buy_price_key']}")
        #                 # #         raise Exception("Could not sell '{}' in pair '{}' for '{}' in Kraken: {}".format(config['trade_amount_krk'], item['pair'], trade_price, result['error']))
        #                 # #     logger.info(result)
        #                 # #     trades.append({'exchange': 'krk', 'orderid': result['result']['txid'][0], 'time': time.time(), 'spread': item['spread']})
        #                 # #
        #                 # #     # Limit order to buy the same amount of pair in Binance
        #                 # #     try:
        #                 # #         result = bnb_exchange.order_limit_buy(symbol=config['bnb_trading_pair'], quantity=config['trade_amount_krk'], price=str(float(min_sell_price)))
        #                 # #         if not result:
        #                 # #             if config['telegram_notifications_on']:
        #                 # #                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['min_sell_price_key']}")
        #                 # #             raise Exception("Could not buy '{}' in pair '{}' for '{}' in Binance.".format(config['trade_amount_krk'], config['bnb_trading_pair'], str(float(min_sell_price))))
        #                 # #     except:
        #                 # #         if config['telegram_notifications_on']:
        #                 # #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']} ). Error when placing limit order in {item['min_sell_price_key']}")
        #                 # #         logger.info(traceback.format_exc())
        #                 # #         raise Exception("Could not buy '{}' in pair '{}' in Binance.".format(config['trade_amount_krk'], config['bnb_trading_pair']))
        #                 # #     logger.info(result)
        #                 # #     trades.append({'exchange': 'bnb', 'orderid': result['orderId'], 'time': time.time(), 'spread': item['spread']})
        #
        #                     # # If there is crypto in Kraken, sell it for USDT in order to set account in initial state
        #                     # tries = 90
        #                     # success = False
        #                     # trades = []
        #                     # while tries >= 0 and not success:
        #                     #     try:
        #                     #         if config['telegram_notifications_on']:
        #                     #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Trying to set account back in initial state...")
        #                     #         logger.info("Trying to limit sell in Kraken to leave account in initial state...")
        #                     #         tries -= 1
        #                     #         # Get decimals info
        #                     #         krk_info = krk_exchange.query_public('AssetPairs', {'pair': item['pair']})
        #                     #         decimals = krk_info['result'][item['pair']]['pair_decimals']
        #                     #         # krk_info = krk_exchange.query_public('AssetPairs', {'pair': candidates[0]['pair']})
        #                     #         # decimals = krk_info['result'][candidates[0]['pair']]['pair_decimals']
        #                     #
        #                     #         krk_balance = krk_exchange.query_private('Balance')
        #                     #         krk_base_currency_available = 0.0
        #                     #         if config['krk_base_currency'] in krk_balance['result']:
        #                     #             krk_base_currency_available = krk_balance['result'][config['krk_base_currency']]
        #                     #         if float(krk_base_currency_available) < 5.0:
        #                     #             success = True
        #                     #             trades = []
        #                     #         else:
        #                     #             krk_tickers = krk_exchange.query_public("Ticker", {'pair': item['pair']})['result'][item['pair']]
        #                     #             # krk_tickers = krk_exchange.query_public("Ticker", {'pair': candidates[0]['pair']})['result'][candidates[0]['pair']]
        #                     #             krk_buy_price = krk_tickers['b'][0]
        #                     #             price = (float(krk_buy_price) + 0.00001)
        #                     #             # price = round(float(krk_buy_price) * 1.0004, decimals)
        #                     #             # krk_sell_price = krk_tickers['a'][0]
        #                     #             # if trend >= 0:
        #                     #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                     #             # else:
        #                     #             #     price = round(float(bnb_buy_price) * 0.99982, 5)
        #                     #             # if float(bnb_buy_price) > float(krk_sell_price):
        #                     #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                     #             # else:
        #                     #             #     price = round(float(krk_sell_price) * 1.0005, 5)
        #                     #             result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                     #             # result_krk = krk_exchange.query_private('AddOrder', {'pair': candidates[0]['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                     #             logger.info(result_krk)
        #                     #             trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': item['pair']})
        #                     #             # trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': candidate['pair']})
        #                     #             limit_order_successful = await short_wait_for_krk_order(trades, config, krk_exchange, logger)
        #                     #             success = limit_order_successful
        #                     #             trades = []
        #                     #     except:
        #                     #         logger.info(traceback.format_exc())
        #                     #         # wait a few seconds before trying again
        #                     #         await asyncio.sleep(10)
        #                     #         continue
        #                     #
        #                     # if not success:
        #                     #     if config['telegram_notifications_on']:
        #                     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Error when selling in Kraken. Could not set account in initial state")
        #                     #     raise Exception(f"[{item['pair']}] Error when selling in Kraken")
        #                     # else:
        #                     #     if config['telegram_notifications_on']:
        #                     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Account back in initial state")
        #
        #                 # Notify Volume in Kraken
        #                 if config['telegram_notifications_on']:
        #                     fee_volume = 0
        #                     for _ in range(5):
        #                         try:
        #                             fee_volume = round(float(krk_exchange.query_private('TradeVolume')['result']['volume']), 2)
        #                             await asyncio.sleep(5)
        #                             break
        #                         except:
        #                             logger.info(traceback.format_exc())
        #                             continue
        #                     telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> Current Volume: {str(fee_volume)} USD")
        #
        #                 # Notify totals
        #                 # Kraken: Get my balances
        #                 kraken_balances = get_kraken_balances(krk_exchange, config)
        #                 logger.info(f"Kraken's Balances\n(Base) {config['krk_base_currency']} balance:{kraken_balances['krk_base_currency_available']} \n(Quote) {config['krk_target_currency']} balance:{kraken_balances['krk_target_currency_available']}\n")
        #
        #
        #                 # Binance: Get my balances
        #                 binance_balances = get_binance_balances(bnb_exchange, config)
        #                 logger.info(f"Binance's Balances\n(Base) {config['bnb_base_currency']} balance:{binance_balances['bnb_base_currency_available']} \n(Quote) {config['bnb_target_currency']} balance:{binance_balances['bnb_target_currency_available']}\n")
        #
        #                 # Log total balances
        #                 total_base_after_trades = round(float(kraken_balances['krk_base_currency_available']) + float(binance_balances['bnb_base_currency_available']), 8)
        #                 total_quote_after_trades = round(float(kraken_balances['krk_target_currency_available']) + float(binance_balances['bnb_target_currency_available']), 2)
        #                 logger.info(f"Total Balances\n(Base) {config['bnb_base_currency']} balance:{str(total_base_after_trades)} \n(Quote) {config['bnb_target_currency']} balance:{str(total_quote_after_trades)}\n")
        #
        #                 # Get Kraken volume
        #                 fee_volume = 0
        #                 for _ in range(5):
        #                     try:
        #                         fee_volume = round(float(krk_exchange.query_private('TradeVolume')['result']['volume']), 2)
        #                         await asyncio.sleep(5)
        #                         break
        #                     except:
        #                         continue
        #                 logger.info(f'New Volume -> {str(fee_volume)}')
        #
        #                 # Compute total diff after trades
        #                 base_diff = round(total_base_after_trades - total_base, 8)
        #                 quote_diff = round(total_quote_after_trades - total_quote, 2)
        #
        #                 # Convert base to quote
        #                 # total_quote_before_trades = round(((float(max_buy_price) - float(min_sell_price)) * total_base) + total_quote, 2)
        #                 # total_quote_after_trades = round(((float(max_buy_price) - float(min_sell_price)) * total_base_after_trades) + total_quote_after_trades, 2)
        #                 # diff = round(total_quote_after_trades - total_quote, 2)
        #
        #                 # Update google sheet
        #                 for _ in range(5):
        #                     try:
        #                         update_google_sheet(config['sheet_id'], config['range_name'], binance_balances['bnb_target_currency_available'], fee_volume)
        #                         break
        #                     except:
        #                         await asyncio.sleep(5)
        #                         continue
        #
        #                 if quote_diff > 0.0:
        #                     logger.info(f"You won {str(abs(quote_diff))} {config['bnb_target_currency']} after last opportunity")
        #                     if config['telegram_notifications_on']:
        #                         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] You won {str(abs(quote_diff))} {config['bnb_target_currency']} after last opportunity")
        #                 elif quote_diff < 0.0:
        #                     logger.info(f"You lost {str(abs(quote_diff))} {config['bnb_target_currency']} after last opportunity")
        #                     if config['telegram_notifications_on']:
        #                         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] You lost {str(abs(quote_diff))} {config['bnb_target_currency']} after last opportunity")
        #
        #                 if item['max_buy_price_key'] == 'bnb' and item['min_sell_price_key'] == 'krk':
        #                     opportunities_bnb_krk_count += 1
        #                 elif item['max_buy_price_key'] == 'krk' and item['min_sell_price_key'] == 'bnb':
        #                     opportunities_krk_bnb_count += 1
        #
        #
        #
        #             except Exception as e: # main try
        #                 logger.info(traceback.format_exc())
        #
        #                 # # If there is XRP in Kraken, sell it for USDT
        #                 # tries = 90
        #                 # success = False
        #                 # trades = []
        #                 # while tries >= 0 and not success:
        #                 #     try:
        #                 #         if config['telegram_notifications_on']:
        #                 #             telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Trying to set account back in initial state...")
        #                 #         logger.info("Trying to limit sell in Kraken to leave account in initial state...")
        #                 #         tries -= 1
        #                 #         # Get decimals info
        #                 #         krk_info = krk_exchange.query_public('AssetPairs', {'pair': item['pair']})
        #                 #         decimals = krk_info['result'][item['pair']]['pair_decimals']
        #                 #         # krk_info = krk_exchange.query_public('AssetPairs', {'pair': candidates[0]['pair']})
        #                 #         # decimals = krk_info['result'][candidates[0]['pair']]['pair_decimals']
        #                 #
        #                 #         krk_balance = krk_exchange.query_private('Balance')
        #                 #         krk_base_currency_available = 0.0
        #                 #         if config['krk_base_currency'] in krk_balance['result']:
        #                 #             krk_base_currency_available = krk_balance['result'][config['krk_base_currency']]
        #                 #         if float(krk_base_currency_available) < 5.0:
        #                 #             success = True
        #                 #             trades = []
        #                 #         else:
        #                 #             krk_tickers = krk_exchange.query_public("Ticker", {'pair': item['pair']})['result'][item['pair']]
        #                 #             # krk_tickers = krk_exchange.query_public("Ticker", {'pair': candidates[0]['pair']})['result'][candidates[0]['pair']]
        #                 #             krk_buy_price = krk_tickers['b'][0]
        #                 #             price = (float(krk_buy_price) + 0.00001)
        #                 #             # price = round(float(krk_buy_price) * 1.0004, decimals)
        #                 #             # krk_sell_price = krk_tickers['a'][0]
        #                 #             # if trend >= 0:
        #                 #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                 #             # else:
        #                 #             #     price = round(float(bnb_buy_price) * 0.99982, 5)
        #                 #             # if float(bnb_buy_price) > float(krk_sell_price):
        #                 #             #     price = round(float(bnb_buy_price) * 1.0005, 5)
        #                 #             # else:
        #                 #             #     price = round(float(krk_sell_price) * 1.0005, 5)
        #                 #             result_krk = krk_exchange.query_private('AddOrder', {'pair': item['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                 #             # result_krk = krk_exchange.query_private('AddOrder', {'pair': candidates[0]['pair'], 'type': 'sell', 'ordertype': 'limit', 'oflags': 'fciq', 'price': price, 'volume': krk_base_currency_available})
        #                 #             logger.info(result_krk)
        #                 #             trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': item['pair']})
        #                 #             # trades.append({'exchange': 'krk', 'orderid': result_krk['result']['txid'][0], 'time': time.time(), 'spread': item['spread'], 'pair': candidate['pair']})
        #                 #             limit_order_successful = await short_wait_for_krk_order(trades, config, krk_exchange, logger)
        #                 #             success = limit_order_successful
        #                 #             trades = []
        #                 #     except:
        #                 #         logger.info(traceback.format_exc())
        #                 #         # wait a few seconds before trying again
        #                 #         await asyncio.sleep(10)
        #                 #         continue
        #                 #
        #                 # if not success:
        #                 #     if config['telegram_notifications_on']:
        #                 #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Error when selling in Kraken")
        #                 #     raise Exception(f"[{item['pair']}] Error when selling in Kraken. Account NOT in initial state!!")
        #                 # else:
        #                 #     if config['telegram_notifications_on']:
        #                 #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{item['pair']}] Limit sell successful, Kraken account back in initial state")
        #
        #                 if item['max_buy_price_key'] == 'bnb' and item['min_sell_price_key'] == 'krk':
        #                     opportunities_bnb_krk_count += 1
        #                 elif item['max_buy_price_key'] == 'krk' and item['min_sell_price_key'] == 'bnb':
        #                     opportunities_krk_bnb_count += 1
        #
        #                 print("\n")
        #                 # opportunities = {'Opportunities_BNB_KRK': opportunities_bnb_krk_count,
        #                 #                  'Opportunities_BNB_CDC': opportunities_bnb_cdc_count,
        #                 #                  'Opportunities_KRK_BNB': opportunities_krk_bnb_count,
        #                 #                  'Opportunities_KRK_CDC': opportunities_krk_cdc_count,
        #                 #                  'Opportunities_CDC_BNB': opportunities_cdc_bnb_count,
        #                 #                  'Opportunities_CDC_KRK': opportunities_cdc_krk_count}
        #                 # for key, value in opportunities.items():
        #                 #     logger.info(f'{key} = {value}')
        #                 logger.info(f"Opportunities = {opportunities_krk_bnb_count}")
        #
        #                 # fee_volume = 0
        #                 # for _ in range(5):
        #                 #     try:
        #                 #         fee_volume = round(float(krk_exchange.query_private('TradeVolume')['result']['volume']), 2)
        #                 #         await asyncio.sleep(5)
        #                 #         break
        #                 #     except:
        #                 #         continue
        #                 # logger.info(f'New Volume -> {str(fee_volume)}')
        #
        #                 logger.info("Exception occurred: Waiting for next iteration... ({} seconds)\n\n\n".format(config['seconds_between_iterations']))
        #                 await asyncio.sleep(config['seconds_between_iterations'])
        #                 continue
        #
        #         # else: # if opportunity_found and not config['safe_mode_on']:
        #         #     if config['telegram_notifications_on']:
        #         #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{config['bnb_trading_pair']}] Opportunity of {str(round(float(item['spread']), 5))} found! (Max buy {item['max_buy_price_key']} | Min sell {item['min_sell_price_key']})")
        #
        #         print("\n")
        #         # opportunities = {'Opportunities_BNB_KRK': opportunities_bnb_krk_count,
        #         #                  'Opportunities_BNB_CDC': opportunities_bnb_cdc_count,
        #         #                  'Opportunities_KRK_BNB': opportunities_krk_bnb_count,
        #         #                  'Opportunities_KRK_CDC': opportunities_krk_cdc_count,
        #         #                  'Opportunities_CDC_BNB': opportunities_cdc_bnb_count,
        #         #                  'Opportunities_CDC_KRK': opportunities_cdc_krk_count}
        #         #
        #         # for key, value in opportunities.items():
        #         #     logger.info(f'{key} = {value}')
        #
        #         logger.info(f"Opportunities = {opportunities_krk_bnb_count}")
        #
        #         # # Averages
        #         # max_buy_price_avg = max_buy_price_avg or float(max_buy_price)
        #         # max_buy_price_avg = round((max_buy_price_avg + float(max_buy_price))/2, 5)
        #         # logger.info(f'Max_buy_price_avg = {str(max_buy_price_avg)}')
        #         # factor = float(max_buy_price)/max_buy_price_avg
        #         # if factor > 1.0006:
        #         #     max_buy_trend += 1
        #         #     logger.info(f'Max buy trend = {max_buy_trend} | trending up hard')
        #         # elif factor > 1.0:
        #         #     max_buy_trend += 1
        #         #     logger.info(f'Max buy trend = {max_buy_trend} | trending up')
        #         # elif factor < 0.9994:
        #         #     max_buy_trend -= 1
        #         #     logger.info(f'Max buy trend = {max_buy_trend} | trending down hard')
        #         # elif factor < 1.0:
        #         #     max_buy_trend -= 1
        #         #     logger.info(f'Max buy trend = {max_buy_trend} | trending down')
        #         # else:
        #         #     pass
        #         # min_sell_price_avg = min_sell_price_avg or float(min_sell_price)
        #         # min_sell_price_avg = round((min_sell_price_avg + float(min_sell_price))/2, 5)
        #         # logger.info(f'Min_sell_price_avg = {str(min_sell_price_avg)}')
        #         # factor = float(min_sell_price)/min_sell_price_avg
        #         # if factor > 1.0006:
        #         #     min_sell_trend += 1
        #         #     logger.info(f'Min sell trend = {min_sell_trend} | trending up hard')
        #         # elif factor > 1.0:
        #         #     min_sell_trend += 1
        #         #     logger.info(f'Min sell trend = {min_sell_trend} | trending up')
        #         # elif factor < 0.9994:
        #         #     min_sell_trend -= 1
        #         #     logger.info(f'Min sell trend = {min_sell_trend} | trending down hard')
        #         # elif factor < 1.0:
        #         #     min_sell_trend -= 1
        #         #     logger.info(f'Min sell trend = {min_sell_trend} | trending down')
        #         # else:
        #         #     pass
        #         #
        #         # spread_avg = spread_avg or float(item['spread'])
        #         # spread_avg = round((spread_avg + float(item['spread']))/2, 6)
        #         # logger.info(f'Spread_avg = {str(spread_avg)}')
        #         # factor = float(float(item['spread']))/spread_avg
        #         # if factor > 1.002297:
        #         #     spread_avg_trend += 1
        #         #     logger.info(f'Spread avg trend = {spread_avg_trend} | trending up hard')
        #         # elif factor > 1.0:
        #         #     spread_avg_trend += 1
        #         #     logger.info(f'Spread avg trend = {spread_avg_trend} | trending up')
        #         # elif factor <= 0.998005:
        #         #     spread_avg_trend -= 1
        #         #     logger.info(f'Spread avg trend = {spread_avg_trend} | trending down hard')
        #         # elif factor < 1.0:
        #         #     spread_avg_trend -= 1
        #         #     logger.info(f'Spread avg trend = {spread_avg_trend} | trending down')
        #         # else:
        #         #     pass
        #
        #         # Volume in Kraken
        #         # fee_volume = 0
        #         # for _ in range(5):
        #         #     try:
        #         #         fee_volume = round(float(krk_exchange.query_private('TradeVolume')['result']['volume']), 2)
        #         #         await asyncio.sleep(5)
        #         #         break
        #         #     except:
        #         #         continue
        #         # logger.info(f'New Volume -> {str(fee_volume)}')
        #
        #         # if spread > config['minimum_spread'] and max_buy_price_key == 'krk':
        #         #     if config['telegram_notifications_on']:
        #         #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{analyzed_pair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #
        #         #Analize other pairs
        #         # for cpair in pairs.keys():
        #         # for cpair in ['ADAUSDT']:
        #         #     # Kraken trading pair ticker
        #         #     if cpair == 'XRPEUR':
        #         #         the_pair = "XXRPZEUR"
        #         #     else:
        #         #         the_pair = cpair
        #         #     krk_tickers = krk_exchange.query_public("Ticker", {'pair': the_pair})['result'][the_pair]
        #         #     krk_buy_price = krk_tickers['b'][0]
        #         #     krk_sell_price = krk_tickers['a'][0]
        #         #     # logger.info(f"\nKRAKEN => Market {item['pair']}\nbuy price: {krk_buy_price} - sell price: {krk_sell_price}\n")
        #         #
        #         #     # Binance trading pair ticker
        #         #     bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #         #     bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == cpair)
        #         #     bnb_buy_price = bnb_ticker['bidPrice']
        #         #     bnb_sell_price = bnb_ticker['askPrice']
        #         #     # logger.info(f"\nBINANCE => Market {config['cpair']}\nbuy price: {bnb_buy_price} - sell price: {bnb_sell_price}\n")
        #         #
        #         #     buy_prices = {'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #         #     # buy_prices = {'cdc': cdc_buy_price, 'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #         #     max_buy_price_key = max(buy_prices, key=buy_prices.get)
        #         #     max_buy_price = buy_prices[max_buy_price_key]
        #         #     sell_prices = {'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #         #     # sell_prices = {'cdc': cdc_sell_price, 'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #         #     min_sell_price_key = min(sell_prices, key=sell_prices.get)
        #         #     min_sell_price = sell_prices[min_sell_price_key]
        #         #     # logger.info(f"Max buy price -> {max_buy_price_key} = {max_buy_price}")
        #         #     # logger.info(f"Min sell price -> {min_sell_price_key} = {min_sell_price}")
        #         #     spread = round(float(max_buy_price) / float(min_sell_price), 8)
        #         #     if max_buy_price_key == 'krk':
        #         #         logger.info(f"[{cpair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #         #     if spread > config['minimum_spread']:
        #         #         if max_buy_price_key == 'krk':
        #         #             # pairs[cpair]['bnb_krk'] += 1
        #         #         # else:
        #         #             pairs[cpair]['krk_bnb'] += 1
        #         #             if config['telegram_notifications_on']:
        #         #                 telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{cpair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #         #
        #         # for key, value in pairs.items():
        #         #     # logger.info(f"{key} BNB_KRK = {value['bnb_krk']}")
        #         #     if key == 'ADAUSDT':
        #         #         logger.info(f"{key} KRK_BNB = {value['krk_bnb']}")
        #     else: # if exchange_is_up:
        #         logger.info("One of the exchanges was down or under maintenance!")
        #
        #
        #     logger.info(f'------------ Iteration {iteration} ------------\n')
        #
        #     if config['test_mode_on']:
        #         await asyncio.sleep(1)
        #         break
        #     else:
        #         # Wait given seconds until next poll
        #         logger.info("Waiting for next iteration... ({} seconds)\n\n\n".format(config['seconds_between_iterations']))
        #         await asyncio.sleep(config['seconds_between_iterations'])
        #
        # except Exception as e:
        #     # buy_prices = {'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #     # max_buy_price_key = max(buy_prices, key=buy_prices.get)
        #     # max_buy_price = buy_prices[max_buy_price_key]
        #     # sell_prices = {'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #     # min_sell_price_key = min(sell_prices, key=sell_prices.get)
        #     # min_sell_price = sell_prices[min_sell_price_key]
        #     # spread = round(float(max_buy_price) / float(min_sell_price), 8)
        #     # logger.info(f"[{analyzed_pair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #     #
        #     # if spread > config['minimum_spread'] and max_buy_price_key == 'krk':
        #     #     if config['telegram_notifications_on']:
        #     #         telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{analyzed_pair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #     #
        #     # try:
        #     #     #Analize other pairs
        #     #     for cpair in pairs.keys():
        #     #         # Kraken trading pair ticker
        #     #         if cpair == 'XRPEUR':
        #     #             the_pair = "XXRPZEUR"
        #     #         else:
        #     #             the_pair = cpair
        #     #         krk_tickers = krk_exchange.query_public("Ticker", {'pair': the_pair})['result'][the_pair]
        #     #         krk_buy_price = krk_tickers['b'][0]
        #     #         krk_sell_price = krk_tickers['a'][0]
        #     #         # logger.info(f"\nKRAKEN => Market {item['pair']}\nbuy price: {krk_buy_price} - sell price: {krk_sell_price}\n")
        #     #
        #     #         # Binance trading pair ticker
        #     #         bnb_tickers = bnb_exchange.get_orderbook_tickers()
        #     #         bnb_ticker = next(item for item in bnb_tickers if item['symbol'] == cpair)
        #     #         bnb_buy_price = bnb_ticker['bidPrice']
        #     #         bnb_sell_price = bnb_ticker['askPrice']
        #     #         # logger.info(f"\nBINANCE => Market {config['cpair']}\nbuy price: {bnb_buy_price} - sell price: {bnb_sell_price}\n")
        #     #
        #     #         buy_prices = {'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #     #         # buy_prices = {'cdc': cdc_buy_price, 'krk': krk_buy_price, 'bnb': bnb_buy_price}
        #     #         max_buy_price_key = max(buy_prices, key=buy_prices.get)
        #     #         max_buy_price = buy_prices[max_buy_price_key]
        #     #         sell_prices = {'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #     #         # sell_prices = {'cdc': cdc_sell_price, 'krk': krk_sell_price, 'bnb': bnb_sell_price}
        #     #         min_sell_price_key = min(sell_prices, key=sell_prices.get)
        #     #         min_sell_price = sell_prices[min_sell_price_key]
        #     #         # logger.info(f"Max buy price -> {max_buy_price_key} = {max_buy_price}")
        #     #         # logger.info(f"Min sell price -> {min_sell_price_key} = {min_sell_price}")
        #     #         spread = round(float(max_buy_price) / float(min_sell_price), 8)
        #     #         logger.info(f"[{cpair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #     #         if spread > config['minimum_spread']:
        #     #             if max_buy_price_key == 'krk':
        #     #                 # pairs[cpair]['bnb_krk'] += 1
        #     #             # else:
        #     #                 pairs[cpair]['krk_bnb'] += 1
        #     #                 if config['telegram_notifications_on'] and max_buy_price_key== 'krk':
        #     #                     telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{cpair}] Max(buy price {max_buy_price_key}) / Min(sell price {min_sell_price_key}) = {spread}\n")
        #     #
        #     #     for key, value in pairs.items():
        #     #         # logger.info(f"{key} BNB_KRK = {value['bnb_krk']}")
        #     #         logger.info(f"{key} KRK_BNB = {value['krk_bnb']}")
        #     # except:
        #     #     continue
        #
        #     logger.info(traceback.format_exc())
        #     # Network issue(s) occurred (most probably). Jumping to next iteration
        #     logger.info("Exception occurred -> '{}'. Waiting for next iteration... ({} seconds)\n\n\n".format(e, config['seconds_between_iterations']))
        #     await asyncio.sleep(config['seconds_between_iterations'])



def get_config():
    config_path = "config/default_config.yaml"
    if exists("config/user_config.yaml"):
        config_path = "config/user_config.yaml"
        print('\n\nUser config detected... checking options ...\n')
    else:
        print('Loading default configuration...')
    config_file = open(config_path)
    data = yaml.load(config_file, Loader=yaml.FullLoader)
    config_file.close()
    return data

def get_kraken_balances(exchange, config):
    krk_balance = exchange.query_private('Balance')
    krk_base_currency_available = 0.0
    if config['krk_base_currency'] in krk_balance['result']:
        krk_base_currency_available = krk_balance['result'][config['krk_base_currency']]
    # Kraken: Get my target currency balance
    krk_target_currency_available = 0.0
    if config['krk_target_currency'] in krk_balance['result']:
        krk_target_currency_available = krk_balance['result'][config['krk_target_currency']]
    return ({'krk_base_currency_available': krk_base_currency_available, 'krk_target_currency_available': krk_target_currency_available})

def get_total_usdt_balance(exchange):
    total_usdt_balance = 0.0
    for _ in range(10):
        try:
            bnb_account = exchange.get_account()
            balances = list(filter(lambda item: float(item['free']) > 0.0 and item['asset'] != "USDT", bnb_account['balances']))
            bnb_tickers = exchange.get_orderbook_tickers()
            for item in balances:
                bnb_ticker = next(pair for pair in bnb_tickers if pair['symbol'] == item['asset'] + "USDT")
                total_usdt_balance += float(bnb_ticker['askPrice']) * float(item['free'])
            usdt_balance = list(filter(lambda item: item['asset'] == "USDT", bnb_account['balances']))
            total_usdt_balance += float(usdt_balance[0]['free'])
            break
        except:
            print(traceback.format_exc())
            time.sleep(5)
            continue
    return round(total_usdt_balance, 2)

def get_binance_balances(exchange, config):
    bnb_balance_result = exchange.get_asset_balance(asset=config['bnb_base_currency'])
    if bnb_balance_result:
        bnb_base_currency_available = round(float(bnb_balance_result['free']) + float(bnb_balance_result['locked']), 8)
    else:
        bnb_base_currency_available = 0.0
    bnb_balance_result = exchange.get_asset_balance(asset=config['bnb_target_currency'])
    if bnb_balance_result:
        bnb_target_currency_available = round(float(bnb_balance_result['free']) + float(bnb_balance_result['locked']), 2)
    else:
        bnb_target_currency_available = 0.0
    return ({'bnb_base_currency_available': bnb_base_currency_available, 'bnb_target_currency_available': bnb_target_currency_available})

def exchange_up(bnb):
    bnb_up = None
    for _ in range(10):
        try:
            bnb_up_result = bnb.get_system_status()
            bnb_up = bnb_up_result and bnb_up_result['status'] == 0 # binance api docs -> 0=normal; 1=system maintenance
            break
        except:
            print(traceback.format_exc())
            time.sleep(5)
            continue
    return bnb_up


def setupLogger(log_filename):
    logger = logging.getLogger('CN')

    file_log_handler = logging.FileHandler(log_filename)
    logger.addHandler(file_log_handler)

    stderr_log_handler = logging.StreamHandler()
    logger.addHandler(stderr_log_handler)

    # nice output format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_log_handler.setFormatter(formatter)
    stderr_log_handler.setFormatter(formatter)

    logger.setLevel('DEBUG')
    return logger

def telegram_bot_sendtext(bot_token, bot_chatID, bot_message):
    send_text = 'https://api.telegram.org/bot' + bot_token + '/sendMessage?chat_id=' + str(bot_chatID) + '&parse_mode=Markdown&text=' + bot_message
    response = requests.get(send_text)

    return response.json()

async def wait_for_orders(trades, config, krk_exchange, bnb_exchange, pair, logger):
    logger.info("Waiting for limit orders...")
    if config['telegram_notifications_on']:
        telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> Waiting for limit orders...")
    # Check if there are trades to cancel
    krk_open_orders = True
    bnb_open_orders = True
    now = time.time()
    start_time = now
    while now - start_time < config['limit_order_time'] and (krk_open_orders or bnb_open_orders):
        logger.info(f"Waiting now {str(int((now - start_time)))} seconds")
        await asyncio.sleep(10)
        tries = 10
        success = False
        while tries >= 0 and not success:
            try:
                tries = tries - 1
                krk_open_orders = krk_exchange.query_private('OpenOrders')['result']['open']
                bnb_open_orders = bnb_exchange.get_open_orders()
                logger.info(f'Kraken open trades: {krk_open_orders}')
                logger.info(f'Binance open trades: {bnb_open_orders}')
                success = True
                await asyncio.sleep(5)
            except:
                logger.info(traceback.format_exc())
                # wait a few seconds before trying again
                await asyncio.sleep(5)
                continue

        now = time.time()

    if not krk_open_orders and not bnb_open_orders:
        logger.info("Limit orders fulfilled")
        if config['telegram_notifications_on']:
            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair}] Limit orders fulfilled")
        return True
    else:
        logger.info(f"Waited more than {config['limit_order_time']} seconds for limit orders...")
        if config['telegram_notifications_on']:
            telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> [{pair}] Waited more than {config['limit_order_time']} seconds for limit orders unsuccessfully")
        return False

async def wait_for_bnb_order(config, bnb_exchange, logger):
    logger.info("Waiting for limit orders...")
    if config['telegram_notifications_on']:
        telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB> Waiting for limit orders...")
    await asyncio.sleep(config['limit_order_time'])
    # Check if there are trades to cancel
    bnb_open_orders = bnb_exchange.get_open_orders()
    if bnb_open_orders:
        return True
    else:
        return False


async def short_wait_for_bnb_order(pair, order_id, config, bnb_exchange, logger):
    logger.info("Waiting for limit order (Binance)...")
    await asyncio.sleep(10)
    # Check if there are trades to cancel
    bnb_open_orders = bnb_exchange.get_open_orders()
    logger.info(f'Binance open trades: {bnb_open_orders}')
    success = True
    if bnb_open_orders:
        for _ in range(50):
            try:
                result_bnb = bnb_exchange.cancel_order(symbol=pair, orderId=order_id)
                logger.info(f"Cancelled order {str(orderid)}")
                success = False
                break
            except BinanceAPIException as e:
                logger.info(e.message)
                if "Unknown order sent" in e.message:
                    success = True
                    break
            except:
                logger.info(traceback.format_exc())
                await asyncio.sleep(5)
                continue
    return success

async def short_wait_for_krk_order(trades, config, krk_exchange, logger):
    logger.info("Waiting for limit order (Kraken)...")
    await asyncio.sleep(15)
    # Check if there are trades to cancel
    krk_open_orders = krk_exchange.query_private('OpenOrders')['result']['open']
    logger.info(f'Kraken open trades: {krk_open_orders}')
    success = True
    if krk_open_orders:
        logger.info(f"Cancelling order {trades[0]['orderid']}")
        for _ in range(50):
            try:
                krk_result = krk_exchange.query_private('CancelAll')
                success = False
                break
            except:
                logger.info(traceback.format_exc())
                await asyncio.sleep(5)
                continue
    return success


async def wait_for_withdrawals(withdrawal_id_krk, withdrawal_id_bnb, config, krk_exchange, bnb_exchange, krk_asset1, krk_asset2, logger):
    logger.info("Waiting for withdrawals...")
    if config['telegram_notifications_on']:
        telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<Arbitrito> Waiting for withdrawals...")

    awaiting_withdrawals = True
    now = time.time()
    start_time = now
    while now - start_time < config['withdrawals_max_wait'] and awaiting_withdrawals:
        logger.info(f"Waiting for withdrawals {str(int(now - start_time))} seconds")
        tries = 10
        success = False
        while tries >= 0 and not success:
            try:
                tries = tries - 1
                # Wait until withdrawals are completed
                # Get withdrawals ids from Withdrawal histories and check its statuses
                krk_result = krk_exchange.query_private('WithdrawStatus', {'asset': krk_asset1})
                krk_success = [item for item in krk_result['result'] if item.get('refid') == withdrawal_id_krk]
                logger.info(f"krk_success -> {krk_success}")
                # bnb_result = bnb_exchange.get_withdraw_history()
                # bnb_success = [item for item in bnb_result['withdrawList'] if item['id'] == withdrawal_id_bnb]
                # logger.info(f"bnb_success -> {bnb_success}")
                # logger.info(f"Statuses -> krk: {krk_success[0]['status']} | bnb: {bnb_success[0]['status']}")
                logger.info(f"Status -> krk: {krk_success[0]['status']}")
                success = True
            except:
                logger.info(traceback.format_exc())
                # wait a few seconds before trying again
                await asyncio.sleep(5)

        # Binance -> status=6 means 'success' in withdrawals from Binance API docs
        # if krk_success[0]['status'] == 'Success' and bnb_success[0]['status'] == 6:
        if krk_success[0]['status'] == 'Success':
            awaiting_withdrawals = False

        if awaiting_withdrawals:
            await asyncio.sleep(20)
            logger.info("Waiting for withdrawals completion...")
        else:
            logger.info("Withdrawals completed.")

        now = time.time()

    # Get deposit tx ids
    # krk_deposit_txid = bnb_success[0]['txId']
    # logger.info(f"Kraken txid =>  {str(krk_deposit_txid)}")
    bnb_deposit_txid = krk_success[0]['txid']
    logger.info(f"Binance txid =>  {str(bnb_deposit_txid)}")

    # Wait until deposits are completed
    awaiting_deposits = True
    now = time.time()
    start_time = now
    krk_success = False
    bnb_success = []
    while now - start_time < config['deposits_max_wait'] and awaiting_deposits:
        logger.info(f"Waiting for deposits {str(int(now - start_time))} seconds")
        tries = 50
        success = False
        while tries >= 0 and not success:
            try:
                tries = tries - 1
                # Get Deposit histories
                # krk_deposit_history = krk_exchange.query_private('DepositStatus', {'asset': krk_asset2})
                # krk_success = [item for item in krk_deposit_history['result'] if item.get('txid') == krk_deposit_txid]
                # logger.info(f"krk_success -> {krk_success}")
                krk_balance = krk_exchange.query_private('Balance')
                krk_available = 0.0
                if krk_asset2 in krk_balance['result']:
                    krk_available = krk_balance['result'][krk_asset2]
                if float(krk_available) > 1000.0:
                    krk_success = True
                bnb_deposit_history = bnb_exchange.get_deposit_history()
                bnb_success = [item for item in bnb_deposit_history['depositList'] if item['txId'] == bnb_deposit_txid]
                logger.info(f"bnb_success -> {bnb_success}")
                success = True
            except:
                logger.info(traceback.format_exc())
                # wait a few seconds before trying again
                await asyncio.sleep(5)

        if krk_success and len(bnb_success) > 0:
            if bnb_success[0]['status'] == 1:
                awaiting_deposits = False

        if awaiting_deposits:
            await asyncio.sleep(20)
            logger.info("Waiting for deposits completion...")
        else:
            logger.info("Deposits completed.")
        now = time.time()

    # Wait 10 seconds more to exchanges to properly set deposits to our accounts...
    await asyncio.sleep(10)

    return not awaiting_deposits

def get_trend(ncandles, krk_exchange, pair, logger):
    for _ in range(20):
        try:
            ohlc = krk_exchange.query_public('OHLC', {'pair': pair, 'interval': '5'})['result'][pair][ncandles*-1:]
            break
        except:
            logger.info(traceback.format_exc())
            continue
    trend = 0
    vwap = float(ohlc[0][5])
    for item in ohlc:
        if float(item[5]) != 0.0:
            if float(item[5]) < vwap:
                trend -= 1
            elif float(item[5]) > vwap:
                trend += 1
            vwap = float(item[5])

    return trend

def get_candidates(krk_exchange, ticker_pairs):
    candidates = []
    for pair in ticker_pairs:
        ohlc_id = int(krk_exchange.query_public('OHLC', {'pair': pair, 'interval': '1440'})['result']['last'])
        ohlc = krk_exchange.query_public('OHLC', {'pair': pair, 'interval': '1440', 'since': ohlc_id})
        ohlc_o1 = ohlc['result'][pair][0][1]
        ohlc_c1 = ohlc['result'][pair][0][4]
        spread = float(ohlc_c1) / float(ohlc_o1)
        if spread > 1.0:
            candidates.append({'pair': pair, 'spread': spread})

    # Sort list by vwap ascending
    sorted_candidates = sorted(candidates, key=lambda k: k['spread'], reverse=True)
    return sorted_candidates


def update_google_sheet(sheet_id, data_range, balance, volume):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    # How the input data should be interpreted.
    value_input_option = 'USER_ENTERED'

    # How the input data should be inserted.
    insert_data_option = 'OVERWRITE'

    data_value = str(time.localtime(time.time())[1]) + '/' + str(time.localtime(time.time())[2]) + '/' + str(time.localtime(time.time())[0])

    value_range_body = {
        "range": data_range,
        "majorDimension": "ROWS",
        "values": [
            [data_value, balance],
        ],
    }

    # append new balance and date
    result = sheet.values().append(spreadsheetId=sheet_id,
                                   range=data_range,
                                   valueInputOption=value_input_option,
                                   insertDataOption=insert_data_option,
                                   body=value_range_body).execute()

    # value_range_body = {
    #     "range": "USDT_26_02_2021!F2:F2",
    #     "majorDimension": "ROWS",
    #     "values": [
    #         [volume],
    #     ],
    # }
    #
    # # update volume
    # result = sheet.values().update(spreadsheetId=sheet_id,
    #                                range="SimulationTest!F2:F2",
    #                                valueInputOption=value_input_option,
    #                                body=value_range_body).execute()

def update_sheet_trades_result(sheet_id, trade_result):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    if trade_result == 'successful':
        data_range = "SimulationTest!F2:F2"
    elif trade_result == 'unsuccessful':
        data_range = "SimulationTest!G2:G2"
    else:
        data_range = "SimulationTest!H2:H2"
    result = sheet.values().get(spreadsheetId=sheet_id,
                                range=data_range).execute()
    values = result.get('values', [])
    updatedValue = str(int(values[0][0]) + 1)

    # Update google sheet with trade result
    # How the input data should be interpreted.
    value_input_option = 'USER_ENTERED'

    value_range_body = {
        "range": data_range,
        "majorDimension": "ROWS",
        "values": [
            [updatedValue],
        ],
    }

    # append new balance and date
    result = sheet.values().update(spreadsheetId=sheet_id,
                                   range=data_range,
                                   valueInputOption=value_input_option,
                                   body=value_range_body).execute()

def update_google_sheet_status(sheet_id, status_message):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    # Update google sheet with status
    # How the input data should be interpreted.
    value_input_option = 'USER_ENTERED'

    data_range = "SimulationTest!F2:F2"

    value_range_body = {
        "range": data_range,
        "majorDimension": "ROWS",
        "values": [
            [status_message],
        ],
    }

    # append new balance and date
    result = sheet.values().update(spreadsheetId=sheet_id,
                                   range=data_range,
                                   valueInputOption=value_input_option,
                                   body=value_range_body).execute()

def update_google_sheet_opps(sheet_id, opps):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    # Update google sheet with status
    # How the input data should be interpreted.
    value_input_option = 'USER_ENTERED'

    data_range = "SimulationTest!G2:G2"

    value_range_body = {
        "range": data_range,
        "majorDimension": "ROWS",
        "values": [
            [opps],
        ],
    }

    # append new balance and date
    result = sheet.values().update(spreadsheetId=sheet_id,
                                   range=data_range,
                                   valueInputOption=value_input_option,
                                   body=value_range_body).execute()

def update_google_sheet_opp_details(sheet_id, opp_details):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    # Update google sheet with status
    # How the input data should be interpreted.
    value_input_option = 'USER_ENTERED'

    # How the input data should be inserted.
    insert_data_option = 'OVERWRITE'

    data_range = "SimulationTest!H3:H1000"

    value_range_body = {
        "range": data_range,
        "majorDimension": "ROWS",
        "values": [
            [opp_details],
        ],
    }

    # append new balance and date
    result = sheet.values().append(spreadsheetId=sheet_id,
                                   range=data_range,
                                   valueInputOption=value_input_option,
                                   insertDataOption=insert_data_option,
                                   body=value_range_body).execute()

def get_balance(sheet_id):
    creds = None
    # The file token.pickle stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

            service = build('sheets', 'v4', credentials=creds)

            # Call the Sheets API
            sheet = service.spreadsheets()

    data_range = "SimulationTest!B2:B5000"
    result = sheet.values().get(spreadsheetId=sheet_id,
                                range=data_range,
                                valueRenderOption='UNFORMATTED_VALUE').execute()
    values = result.get('values', [])
    lastBalance = values[-1][0]
    return float(lastBalance)

def get_1d_tech_info(pair):
    this1DRsi = None
    this1DStochFFastK = None
    this1DStochFFastD = None
    taapi_symbol = pair.split('USDT')[0] + "/" + "USDT"
    endpoint = "https://api.taapi.io/bulk"

    parameters = {
        "secret": config['taapi_api_key'],
        "construct": {
            "exchange": "binance",
            "symbol": taapi_symbol,
            "interval": "1d",
            "indicators": [
            {
                # Current Relative Strength Index
                "id": "thisrsi",
                "indicator": "rsi"
            },
            {
                # Current stoch fast
                "id": "thisstochf",
                "indicator": "stochf",
                "optInFastK_Period": 3,
                "optInFastD_Period": 3
            }
            ]
        }
    }

    for _ in range(5):
        try:
            # Send POST request and save the response as response object
            response = requests.post(url = endpoint, json = parameters)

            # Extract data in json format
            result = response.json()

            this1DRsi = float(result['data'][0]['result']['value'])
            this1DStochFFastK = float(result['data'][1]['result']['valueFastK'])
            this1DStochFFastD = float(result['data'][1]['result']['valueFastD'])
            break
        except:
            logger.info(f"{pair} | TAAPI Response (1D): {response.reason}. Trying again...")
            time.sleep(2)
            continue
    return this1DRsi, this1DStochFFastK, this1DStochFFastD

def get_2h_tech_info(pair):
    this2HRsi = None
    this2HStochFFastK = None
    this2HStochFFastD = None
    prev2HStochFFastD = None
    this2HBolinger = None
    taapi_symbol = pair.split('USDT')[0] + "/" + "USDT"
    endpoint = "https://api.taapi.io/bulk"

    parameters = {
        "secret": config['taapi_api_key'],
        "construct": {
            "exchange": "binance",
            "symbol": taapi_symbol,
            "interval": "2h",
            "indicators": [
            {
                # Current Relative Strength Index
                "id": "thisrsi",
                "indicator": "rsi"
            },
            {
                # Current stoch fast
                "id": "thisstochf",
                "indicator": "stochf",
                "optInFastK_Period": 3,
                "optInFastD_Period": 3
            },
            {
                # Previous stoch fast
                "id": "prevstochf",
                "indicator": "stochf",
                "backtrack": 1,
                "optInFastK_Period": 3,
                "optInFastD_Period": 3
            },
            {
                # Current Bolinger bands
                "id": "thisbb",
                "indicator": "bbands2"
            }
            ]
        }
    }

    for _ in range(5):
        try:
            # Send POST request and save the response as response object
            response = requests.post(url = endpoint, json = parameters)

            # Extract data in json format
            result = response.json()

            this2HRsi = float(result['data'][0]['result']['value'])
            this2HStochFFastK = float(result['data'][1]['result']['valueFastK'])
            this2HStochFFastD = float(result['data'][1]['result']['valueFastD'])
            prev2HStochFFastD = float(result['data'][2]['result']['valueFastD'])
            this2HBolinger = float(result['data'][3]['result']['valueLowerBand'])
            time.sleep(3)
            break
        except:
            logger.info(f"{pair} | TAAPI Response (2H): {response.reason}. Trying again...")
            time.sleep(2)
            continue
    return this2HRsi, this2HStochFFastK, this2HStochFFastD, prev2HStochFFastD, this2HBolinger

def twoh_trading_break(a_date):
    return a_date.weekday() == 6 and a_date.hour > 1 and a_date.hour < 23

loop = asyncio.get_event_loop()
try:
    logger = setupLogger('logfile.log')
    config = get_config()
    loop.run_until_complete(main(config))
except KeyboardInterrupt:
    pass
finally:
    print("Stopping YATB...")
    # Update google sheet status field
    dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    statusMessage = f"{dateStamp} -- Bot stopped"
    for _ in range(5):
        try:
            update_google_sheet_status(config['sheet_id'], statusMessage)
            break
        except:
            time.sleep(3)
            continue
    loop.close()
