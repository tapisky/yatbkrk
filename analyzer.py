#!/usr/bin/env python3
import asyncio
import aiohttp
import time
import logging
import yaml
import sys
import traceback
import json
import krakenex
import os.path
import requests
import datetime
from random import sample
from os.path import exists

from datetime import datetime as datetime_helper
from datetime import timedelta
from requests import Request, Session
from requests.exceptions import ConnectionError, Timeout, TooManyRedirects
from os.path import exists
from yatbkrk_constants import *

class Analyzer:
    def __init__(self, config, krk_exchange, logger):
        self.config = config
        self.krk_exchange = krk_exchange
        self.logger = logger

    async def get_opportunities(self):
        # Get analysis asset pairs
        sorted_asset_pairs = {}
        self.logger.info("Getting list of asset pairs to analyze...")

        url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest'
        parameters = {
            'start':'1',
            'limit':'100',
            'convert':'USD',
            'market_cap_min': 1500000000,
            'volume_24h_min': 35000000
        }
        headers = {
            'Accepts': 'application/json',
            'X-CMC_PRO_API_KEY': self.config['cmc_api_key']
        }

        session = Session()
        session.headers.update(headers)

        done = False
        for _ in range(5):
            try:
                response = session.get(url, params=parameters)
                data = json.loads(response.text)
                done = True
                break
            except (ConnectionError, Timeout, TooManyRedirects) as e:
                self.logger.info(e)
                print("Retrying after 10 seconds...")
                time.sleep(10)
                continue
        if done:
            targets = data['data']
            target_symbols = list(map(lambda x: x['symbol'], targets))
            target_symbols = [item for item in target_symbols if item not in STABLE_COINS]

            # Get Kraken asset pairs and filter/sort them according to CoinMarketCap list
            self.logger.info("Getting filtered and sorted asset pairs list from Kraken according to CoinMarketCap list...")
            done = False
            for _ in range(10):
                try:
                    krk_asset_pairs = await self.krk_exchange.query_public('AssetPairs')
                    done = True
                    break
                except:
                    await asyncio.sleep(2)
                    continue
            if done:
                krk_usd_asset_pairs = [x for x in krk_asset_pairs['result'] if "USD" in x]
                krk_processed_usd_assets_pairs = {key: krk_asset_pairs['result'][key]['wsname'] for key in krk_usd_asset_pairs}
                krk_filtered_usd_asset_pairs = {k: v for k, v in krk_processed_usd_assets_pairs.items() if not any(sub in v for sub in STABLE_FIAT_COINS)}
                krk_filtered_usd_asset_pairs = {k: v.replace("/", "") for k, v in krk_filtered_usd_asset_pairs.items()}
                for target_symbol in target_symbols:
                    if "BTC" in target_symbol:
                        if any("XBTUSD" in v for v in krk_filtered_usd_asset_pairs.values()):
                            sorted_asset_pairs['BTCUSD'] = [k for k,v in krk_filtered_usd_asset_pairs.items() if v == 'XBTUSD'][0]
                    else:
                        t = target_symbol + "USD"
                        if any(t in v for v in krk_filtered_usd_asset_pairs.values()):
                            sorted_asset_pairs[t] = [k for k,v in krk_filtered_usd_asset_pairs.items() if v == t][0]
        return sorted_asset_pairs

    async def analyze_pair(self, pair, kraken_pair, interval, opps):
        # Initialize variables
        prevKlineClose = None
        prevKlineLow = None
        thisKlineClose = None
        thisKlineLow = None
        prevRsi = None
        thisRsi = None
        prevStochFFastK = None
        prevStochFFastD = None
        thisStochFFastK = None
        thisStochFFastD = None

        for _ in range(5):
            try:
                await asyncio.sleep(sample(NONCE,1)[0])
                klines = await self.krk_exchange.query_public('OHLC', {'pair': pair, 'interval': KRK_INTERVALS[interval]})
                klines = klines['result'][kraken_pair][-1]
                await asyncio.sleep(sample(NONCE,1)[0])
                prev_klines = await self.krk_exchange.query_public('OHLC', {'pair': pair, 'interval': KRK_INTERVALS[interval]})
                prev_klines = prev_klines['result'][kraken_pair][-2]
                if float(prev_klines[4]) - float(prev_klines[1]) < 0:
                    prevKline = 'negative'
                else:
                    prevKline = 'positive'
                if float(klines[4]) - float(klines[1]) < 0:
                    thisKline = 'negative'
                else:
                    thisKline = 'positive'
                prevKlineClose = float(prev_klines[4])
                prevKlineLow = float(prev_klines[3])
                thisKlineClose = float(klines[4])
                thisKlineLow = float(klines[3])
                break
            except:
                # self.logger.info(traceback.format_exc())
                # self.logger.info(f"Retrying... ({pair})")
                await asyncio.sleep(2)
                continue

        # Get technical info
        taapiSymbol = pair.split('USD')[0] + "/" + "USDT"
        endpoint = "https://api.taapi.io/bulk"

        # Get  indicators
        parameters = {
            "secret": self.config['taapi_api_key'],
            "construct": {
                "exchange": "binance",
                "symbol": taapiSymbol,
                "interval": interval,
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

        async with aiohttp.ClientSession() as session:
            done = False
            for _ in range(20):
                try:
                    # Send POST request and save the response as response object
                    # response = requests.post(url = endpoint, json = parameters)
                    async with session.post(url = endpoint, json = parameters) as resp:
                        # Extract data in json format
                        result = await resp.json()
                    prevRsi = float(result['data'][0]['result']['value'])
                    thisRsi = float(result['data'][1]['result']['value'])
                    prevStochFFastK = float(result['data'][2]['result']['valueFastK'])
                    prevStochFFastD = float(result['data'][2]['result']['valueFastD'])
                    thisStochFFastK = float(result['data'][3]['result']['valueFastK'])
                    thisStochFFastD = float(result['data'][3]['result']['valueFastD'])
                    done = True
                    break
                except:
                    # self.logger.info(traceback.format_exc())
                    # self.logger.info(f"{pair} | TAAPI Response ({str(interval)}): {result}. Trying again...")
                    await asyncio.sleep(8)
                    await asyncio.sleep(sample(NONCE,1)[0])
                    continue
        if done:
            self.logger.info(f"{str(interval)} Data: {pair} | This RSI {str(round(thisRsi, 2))} | Prev StochF K,D {str(round(prevStochFFastK, 2))}, {str(round(prevStochFFastD, 2))} | This StochF K,D {str(round(thisStochFFastK, 2))}|{str(round(thisStochFFastD, 2))}")
            # Check if good opp
            if (
                (thisRsi < 53.0
                and thisStochFFastK < 14.5
                and thisStochFFastK < thisStochFFastD
                and thisStochFFastD > 22.0
                and prevStochFFastK < 78.0
                and prevStochFFastK < prevStochFFastD
                and prevStochFFastK - thisStochFFastK < 20.0)
                or (
                    (prevStochFFastK < prevStochFFastD
                    and thisStochFFastK > thisStochFFastD
                    and thisStochFFastK > 60.0
                    and thisStochFFastK < 99.0
                    and thisStochFFastK - thisStochFFastD > (thisStochFFastK * 0.275)
                    and thisRsi < 55.0)
                )
            ):
                # Put  opportunities in opps dict
                opps.append({'pair': pair, 'krk_pair': kraken_pair, 'interval': interval, 'priority': PRIORITIES[interval]})
                self.logger.info(f"{pair} good candidate for one of the strategies")
            else:
                self.logger.info(f"{pair} not a good entry point")
        else:
            self.logger.info(f"{str(interval)} Data: {pair} | Could not complete analysis")
