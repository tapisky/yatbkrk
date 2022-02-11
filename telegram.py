#!/usr/bin/env python3
import requests
import traceback
import time

class Telegram:
    """Configures Telegram object with info from the provided config"""
    def __init__(self, config=None, logger=None):
        self.url = 'https://api.telegram.org/bot'
        self.action = '/sendMessage?chat_id='
        self.params = '&parse_mode=Markdown&text='
        self.bot_token = config['telegram_bot_token']
        self.bot_chatId = config['telegram_user_id']
        self.notifications_on = config['telegram_notifications_on']
        self.logger = logger


    def send(self, bot_message):
        for _ in range(5):
            try:
                send_text = self.url + self.bot_token + self.action + str(self.bot_chatId) + self.params + bot_message
                response = requests.get(send_text)
                return response.json()
            except:
                self.logger.info(e)
                print("Retrying after 10 seconds...")
                time.sleep(10)
                continue
