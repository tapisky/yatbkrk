import json
import pickle
import os
import time
import traceback

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

class SheetsHelper:
    def __init__(self, sheet_id, logger):
        self.sheet_id = sheet_id
        self.sheet = None
        self.logger = logger
        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)

                service = build('sheets', 'v4', credentials=creds)

                # Call the Sheets API
                self.sheet = service.spreadsheets()

    def update_row(self, data_range, value):
        # How the input data should be interpreted.
        value_input_option = 'USER_ENTERED'

        # How the input data should be inserted.
        insert_data_option = 'OVERWRITE'

        value_range_body = {
            "range": data_range,
            "majorDimension": "ROWS",
            "values": [
                [value],
            ],
        }

        try:
            # append new value and date
            result = self.sheet.values().update(spreadsheetId=self.sheet_id,
                                                range=data_range,
                                                valueInputOption=value_input_option,
                                                body=value_range_body).execute()
        except:
            self.logger.info(traceback.format_exc())

    def append_row(self, data_range, action_date, value):
        # How the input data should be interpreted.
        value_input_option = 'USER_ENTERED'

        # How the input data should be inserted.
        insert_data_option = 'OVERWRITE'

        value_range_body = {
            "range": data_range,
            "majorDimension": "ROWS",
            "values": [
                [action_date, value],
            ],
        }

        try:
            # append new value and date
            result = self.sheet.values().append(spreadsheetId=self.sheet_id,
                                           range=data_range,
                                           valueInputOption=value_input_option,
                                           insertDataOption=insert_data_option,
                                           body=value_range_body).execute()
        except:
            self.logger.info(traceback.format_exc())

    def get_cell_value(self, cell):
        value = ""
        for _ in range(5):
            try:
                result = self.sheet.values().get(spreadsheetId=self.sheet_id,
                                            range=cell,
                                            valueRenderOption='UNFORMATTED_VALUE').execute()
                values = result.get('values', [])
                value = values[-1][0]
                break
            except:
                self.logger.info(traceback.format_exc())
                time.sleep(5)
                continue
        return value
