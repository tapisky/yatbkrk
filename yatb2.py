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
from asynckraken import Client
from analyzer import Analyzer
from sheetshelper import SheetsHelper
from telegram import Telegram
from simulator import Simulator

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
    krk_exchange = Client(key=config['krk_api_key'], secret=config['krk_api_secret'])

    # Initialize Google sheets
    google_sheets_helper = SheetsHelper(config['sheet_id'], logger)

    # Initialize Telegram
    telegram = Telegram(config=config)
    if telegram.notifications_on:
        telegram.send("YATB SIM - Bot Started")

    # Initialize Sorted pairs
    sorted_pairs = {}

    # Initialize Simulator
    simulator = Simulator(krk_exchange, logger, telegram)

    # Initialize sim_trades
    sim_trades = 1

    date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    status_message = f"{date_stamp} -- Bot Started"
    google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)

    while True:
        date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
        status_message = f"{date_stamp} -- Checking current trades..."
        google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
        for trade in trades:
            if config['sim_mode_on']:
                # Get current candle high and low prices of trade['pair']
                for _ in range(5):
                    try:
                        klines = await self.krk_exchange.query_public('OHLC', {'pair': trade['pair'], 'interval': trade['interval']})
                        klines = klines['result'][kraken_pair][-1]
                        krk_tickers = await self.krk_exchange.query_public("Ticker", {'pair': trade['pair']})
                        if krk_tickers['error'] != []:
                            raise
                        krk_tickers_result = krk_tickers['result'][pair]
                        krk_sell_price = krk_tickers_result['a'][0]
                        break
                    except:
                        logger.info("Retrying...")
                        await asyncio.sleep(3)
                        continue
                if klines[2] >= float(trade['expsellprice']) and time.time() - trade['time'] > 90:
                    # Success!
                    trade['status'] = 'remove'
                    trade['result'] = 'successful'
                    volume_30d = float(google_sheets_helper.get_cell_value('SimulationTest!T2:T2'))
                    quantity = round((float(trade['quantity']) * float(krk_sell_price)) * (1.0 - self.get_krk_fees(account_30d_volume)), 5)
                    # actual_volume = float(trade['expsellprice']) * float(trade['quantity']) * (1.0 - float(simulator.get_krk_fees(volume_30d)))
                    google_sheets_helper.update_row('SimulationTest!T2:T2', volume_30d + quantity)
            if trade['status'] == 'remove':
                if config['sim_mode_on']:
                    balance = float(google_sheets_helper.get_cell_value('SimulationTest!B2:B5000'))
                    profit = quantity - balance
                # else:
                    # balance = get_total_usdt_balance(bnb_exchange)
                    # profit = round(float(balance) - float(get_balance(config['sheet_id'])), 2)
                result_text = "won" if profit > 0 else "lost"
                log_message = f"<YATB> [{trade['pair']}] ({trade['result'].upper()}) You have {result_text} {str(round(float(profit), 2))} USD"
                logger.info(log_message)
                if telegram.notifications_on:
                    telegram.send(log_message)
                # Update google sheet
                sheets_date = str(time.localtime(time.time())[1]) + '/' + str(time.localtime(time.time())[2]) + '/' + str(time.localtime(time.time())[0])
                sheetshelper.append_row('SimulationTest!A1:A5000', sheets_date, round(quantity, 2))
                opp_details = f"{trade['pair']} - {trade['interval']}"
                sheetshelper.update_row('SimulationTest!G2:G2', opp_details)
            else:
                # Current trade is still valid
                logger.info(f"<YATB> [{trade['pair']}] Trade ongoing:")
                print(trade)

        # Remove old trade items
        trades = list(filter(lambda item: item['status'] == 'active', trades))
        sim_trades = config['sim_trades'] - len(trades)

        if time.gmtime()[4] % 15 >= 13:
            date_stamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
            status_message = f"{date_stamp} -- Checking opportunities..."
            google_sheets_helper.update_row('SimulationTest!F2:F2', status_message)
            logger.info("Starting analysis...")
            opportunities = []
            # Get sorted pairs first
            analyzer = Analyzer(config, krk_exchange, logger)
            s = await analyzer.get_opportunities()
            if s != {}:
                sorted_pairs = s
                print(s)
            tasks = []
            for pair in sorted_pairs:
                tasks.append(asyncio.ensure_future(analyzer.analyze_pair(pair, sorted_pairs[pair], 15, opportunities)))
            await asyncio.gather(*tasks)
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
                    trade_amount = float(google_sheets_helper.get_cell_value('SimulationTest!B2:B5000'))
                    volume_30d = float(google_sheets_helper.get_cell_value('SimulationTest!T2:T2'))
                    sim_result = await simulator.simulate_limit_buy_sell(opportunities[0]['pair'], opportunities[0]['krk_pair'], trade_amount, volume_30d, trades)
                    sim_trades -= 1
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
            status_message = f"{date_stamp} -- Waiting for next iteration..."
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

def telegram_bot_sendtext(bot_token, bot_chatID, bot_message):
    send_text = 'https://api.telegram.org/bot' + bot_token + '/sendMessage?chat_id=' + str(bot_chatID) + '&parse_mode=Markdown&text=' + bot_message
    response = requests.get(send_text)

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
