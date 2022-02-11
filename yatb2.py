#!/usr/bin/env python3
import asyncio
import time
import logging
import yaml
import sys
import traceback
import json
import pickle
import os.path
import requests
import datetime

from datetime import datetime as datetime_helper
from datetime import timedelta
from requests import Request, Session
from requests.exceptions import ConnectionError, Timeout, TooManyRedirects
from os.path import exists
from analyzer import Analyzer
from sheetshelper import SheetsHelper
from telegram import Telegram
from simulator import Simulator
from yatbkrk_constants import *
from exchange import Exchange

class Opportunities:
    def __init__(self):
        self.opps_list = []

    def append(self, element):
        self.opps_list.append(element)

    def clear(self):
        self.opps_list = []

async def main(config):
    iteration = 0

    # Initialize opportunities
    opportunities = []

    # Initialize trades
    trades = []

    # Kraken API setup
    exchange = Exchange(config['krk_api_key'], config['krk_api_secret'], logger)

    # Initialize Analyzer
    analyzer = Analyzer(config, exchange.krk, logger)

    # Initialize Google sheets
    google_sheets_helper = SheetsHelper(config['sheet_id'], logger)

    # Initialize Telegram
    telegram = Telegram(config=config, logger=logger)
    if telegram.notifications_on:
        telegram.send("YATB SIM - Bot Started")

    # Initialize Sorted pairs
    sorted_pairs = {}

    # Initialize Simulator
    simulator = Simulator(exchange.krk, google_sheets_helper, logger, telegram)

    # Initialize sim_trades
    sim_trades = config['sim_trades']

    # Initialize trade_amount for simulation
    available = round(float(google_sheets_helper.get_cell_value('SimulationTest!B2:B5000')), 2)
    google_sheets_helper.update_row('SimulationTest!P2:P2', available)

    date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    status_message = f"{date_stamp} -- Bot Started"
    google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)

    while True:
        if trades:
            date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
            status_message = f"{date_stamp} -- Checking current trades..."
            google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
            logger.info(f"Checking current trades: {trades}")
        for trade in trades:
            if config['sim_mode_on']:
                # Get current candle high and low prices of trade['pair']
                for _ in range(5):
                    try:
                        klines = await exchange.krk.query_public('OHLC', {'pair': trade['pair'], 'interval': KRK_INTERVALS[trade['interval']]})
                        if klines['error'] != []:
                            raise
                        klines = klines['result'][list(klines['result'].keys())[0]][-1]
                        # krk_asset_pair = await exchange.krk.query_public("AssetPairs", {'pair': trade['pair']})
                        # if krk_asset_pair['error'] != []:
                        #     raise
                        # else:
                        #     krk_asset_pair = krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]
                        # pair_decimals = krk_asset_pair['pair_decimals']
                        # print(f'[{pair}] Pair Decimals {str(pair_decimals)}')
                        # lot_decimals = krk_asset_pair['lot_decimals']
                        # print(f'[{pair}] Lot Decimals {str(lot_decimals)}')
                        # order_min = len(krk_asset_pair['ordermin'].split(".")[1])
                        # print(f'[{pair}] Order Min Decimals {str(order_min)}')
                        krk_tickers = await exchange.krk.query_public("Ticker", {'pair': trade['pair']})
                        if krk_tickers['error'] != []:
                            raise
                        krk_tickers = krk_tickers['result'][list(krk_tickers['result'].keys())[0]]
                        krk_sell_price = krk_tickers['a'][0]
                        break
                    except:
                        logger.info(traceback.format_exc())
                        logger.info("Retrying...")
                        await asyncio.sleep(3)
                        continue
                if (float(klines[2]) >= float(trade['expsellprice'])) and ((time.time() - trade['time']) > TRADE_CHECK_WAIT_TIME[trade['interval']]):
                    # Success!
                    trade['status'] = 'remove'
                    trade['result'] = 'successful'
                    volume_30d = round(float(google_sheets_helper.get_cell_value('SimulationTest!T2:T2')), 2)
                    quantity = round((float(trade['quantity']) * float(trade['expsellprice'])) * (1.0 - simulator.get_krk_fees(volume_30d)), 2)
                    google_sheets_helper.update_row('SimulationTest!T2:T2', volume_30d + quantity)
                    available = round(float(google_sheets_helper.get_cell_value('SimulationTest!P2:P2')), 2)
                    google_sheets_helper.update_row('SimulationTest!P2:P2', available + quantity)
                elif (time.time() - trade['time'] > TRADE_EXPIRY_TIME[trade['interval']]):
                    trade['status'] = 'remove'
                    trade['result'] = 'unsuccessful'
                    volume_30d = round(float(google_sheets_helper.get_cell_value('SimulationTest!T2:T2')), 2)
                    sell_price = await exchange.krk.get_sell_price(trade['pair'])
                    quantity = round(float(trade['quantity']) * float(sell_price) * (1.0 - simulator.get_krk_fees(volume_30d)), 2)
                    google_sheets_helper.update_row('SimulationTest!T2:T2', volume_30d + quantity)
                    available = round(float(google_sheets_helper.get_cell_value('SimulationTest!P2:P2')), 2)
                    google_sheets_helper.update_row('SimulationTest!P2:P2', available + quantity)
            if trade['status'] == 'remove':
                if config['sim_mode_on']:
                    balance = float(google_sheets_helper.get_cell_value('SimulationTest!B2:B5000'))
                    # profit = quantity - balance
                    # else:
                        # balance = get_total_usdt_balance(bnb_exchange.krk)
                        # profit = round(float(balance) - float(get_balance(config['sheet_id'])), 2)
                    result_text = "won" if profit > 0 else "lost"
                    log_message = f"<YATB> [{trade['pair']}] ({trade['result'].upper()}) You have {result_text} {str(round(float(trade['profit']), 2))} USD. 30d balance = {volume_30d + quantity}"
                    logger.info(log_message)
                    if telegram.notifications_on:
                        telegram.send(log_message)
                    # Update google sheet
                    sheets_date = str(time.localtime(time.time())[1]) + '/' + str(time.localtime(time.time())[2]) + '/' + str(time.localtime(time.time())[0])
                    google_sheets_helper.append_row('SimulationTest!A1:A5000', sheets_date, balance + trade['profit'])
                    opp_details = f"{trade['pair']} - {trade['interval']}"
                    google_sheets_helper.append_row('SimulationTest!H3:H5000', opp_details, "")
            else:
                # Current trade is still valid
                logger.info(f"<YATB> [{trade['pair']}] Trade ongoing:")
                print(trade)

        # Remove old trade items
        trades = list(filter(lambda item: item['status'] == 'active', trades))
        sim_trades = config['sim_trades'] - len(trades)

        # Check markets if it's the right time
        intervals = []
        if time.gmtime()[3] % 24 == 23 and time.gmtime()[4] == 58:
            intervals.append('1d')
        if time.gmtime()[3] % 4 == 3 and time.gmtime()[4] == 58:
            intervals.append('4h')
        if (time.gmtime()[4] % 15 == 13) or (time.gmtime()[4] % 15 == 14 and time.gmtime()[5] < 10):
            intervals.append('15m')

        if intervals:
            date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
            status_message = f"{date_stamp} -- Checking opportunities for intervals => {intervals}..."
            google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
            logger.info("Starting analysis...")
            opportunities = []
            # Get sorted pairs first
            s = await analyzer.get_opportunities()
            if s != {}:
                sorted_pairs = s
                print(s)
            tasks = []
            for pair in sorted_pairs:
                for interval in intervals:
                    tasks.append(asyncio.ensure_future(analyzer.analyze_pair(pair, sorted_pairs[pair], interval, opportunities)))
            await asyncio.gather(*tasks)
            #  Sort opportunities by priority
            opportunities = sorted(opportunities, key=lambda k: k['priority'])
            print(f'Opps => {opportunities}')
            if opportunities:
                date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
                status_message = f"{date_stamp} -- Opps found -> {opportunities}"
                google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
                log_message = f"<YATB KRK SIM> Opps found => {opportunities}"
                if telegram.notifications_on:
                    telegram.send(log_message)
                if sim_trades > 0:
                    print("Simulating limit buy and sell...")
                    # Get opportunities with higher priority (higher priority = 1)
                    min_priority = opportunities[0]['priority']
                    filtered_opportunities = list(filter(lambda x: x['priority'] == min_priority, opportunities))
                    available = round(float(google_sheets_helper.get_cell_value('SimulationTest!P2:P2')), 2)
                    trade_amount = available / sim_trades
                    total_trades_amount = 0
                    for i in range(sim_trades):
                        sim_result = await simulator.simulate_limit_buy_sell(filtered_opportunities[i % len(filtered_opportunities)]['pair'], filtered_opportunities[i % len(filtered_opportunities)]['krk_pair'], trade_amount, filtered_opportunities[i % len(filtered_opportunities)]['interval'], trades)
                        total_trades_amount += sim_result
                        # sim_result = await simulator.simulate_limit_buy_sell(opp[0]['pair'], opp[0]['krk_pair'], opp_trade_amount, opp[0]['interval'], trades)
                        google_sheets_helper.update_row('SimulationTest!P2:P2', available - trade_amount)
                        sim_trades -= 1
                    # Update 30d trade amount
                    volume_30d = round(float(google_sheets_helper.get_cell_value('SimulationTest!T2:T2')), 2)
                    google_sheets_helper.update_row('SimulationTest!T2:T2', volume_30d + total_trades_amount)
                else:
                    log_message = f"<YATB KRK SIM> No trades available"
                    if telegram.notifications_on:
                        telegram.send(log_message)
            else:
                log_message = f"<YATB KRK SIM> No opps found"
                if telegram.notifications_on:
                    telegram.send(log_message)
        else:
            date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
            status_message = f"{date_stamp} -- Waiting for next iteration -- Sim trades = {sim_trades} -- Ongoing trades: {trades}"
            google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
            logger.info("Waiting for the right time...")
        await asyncio.sleep(30)

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
    # date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    # status_message = f"{date_stamp} -- Bot stopped"
    # for _ in range(5):
    #     try:
    #         update_google_sheet_status(config['sheet_id'], status_message)
    #         break
    #     except:
    #         time.sleep(3)
    #         continue
    loop.close()
