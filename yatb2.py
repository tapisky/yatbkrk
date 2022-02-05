#!/usr/bin/env python3
import asyncio
import time
import logging
import yaml
import sys
import traceback
import json
import cryptocom.exchange as cro
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
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from analyzer import Analyzer

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
    opportunities = Opportunities()

    # Kraken API setup
    krk_exchange = Client(key=config['krk_api_key'], secret=config['krk_api_secret'])

    while True:
        if time.gmtime()[4] % 15 >= 13:
            logger.info("Starting analysis...")
            opportunities.clear()

            # Get sorted pairs first
            analyzer = Analyzer(config, krk_exchange, logger)
            sorted_pairs = await analyzer.get_opportunities()
            print(sorted_pairs)

            tasks = []
            for pair in sorted_pairs:
                tasks.append(asyncio.ensure_future(analyzer.analyze_pair(pair, sorted_pairs[pair], 15, opportunities)))
            await asyncio.gather(*tasks)
            print(f'Opps => {opportunities.opps_list}')
            if opportunities.opps_list:
                telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB KRK SIM> Opps found => {opportunities.opps_list}")
            else:
                telegram_bot_sendtext(config['telegram_bot_token'], config['telegram_user_id'], f"<YATB KRK SIM> No opps found")
        else:
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
    # dateStamp = datetime_helper.utcnow().strftime("%d/%m/%Y %H:%M:%S")
    # statusMessage = f"{dateStamp} -- Bot stopped"
    # for _ in range(5):
    #     try:
    #         update_google_sheet_status(config['sheet_id'], statusMessage)
    #         break
    #     except:
    #         time.sleep(3)
    #         continue
    loop.close()
