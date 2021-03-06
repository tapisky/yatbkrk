import time
import traceback
import asyncio
from asynckraken import Client

class Exchange:
    def __init__(self, api_key, api_secret, logger):
        """Configures Exchange object with keys and logger for async Kraken exchange"""
        self.krk = Client(key=api_key, secret=api_secret)
        self.logger = logger

    async def get_sell_price(self, pair):
        result = 0
        # Get bid/ask prices
        for _ in range(20):
            try:
                krk_asset_pair = await self.krk.query_public("AssetPairs", {'pair': pair})
                if krk_asset_pair['error'] != []:
                    raise
                pair_decimals = krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['pair_decimals']
                self.logger.info(f'[{pair}] Pair Decimals {str(pair_decimals)}')
                lot_decimals = krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['lot_decimals']
                self.logger.info(f'[{pair}] Lot Decimals {str(lot_decimals)}')
                # order_min = len(krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['ordermin'].split(".")[1])
                # self.logger.info(f'[{pair}] Order Min Decimals {str(order_min)}')
                krk_tickers = await self.krk.query_public("Ticker", {'pair': pair})
                if krk_tickers['error'] != []:
                    raise
                krk_tickers_result = krk_tickers['result'][list(krk_tickers['result'].keys())[0]]
                krk_sell_price = krk_tickers_result['a'][0]
                self.logger.info(f'[{pair}] (UNSUCCESSFUL TRADE) Sell price {krk_sell_price}')
                decimal_formatter = "%." + str(pair_decimals) + "f"
                lot_decimal_formatter = "%." + str(lot_decimals) + "f"
                krk_sell_price = (decimal_formatter % float(krk_sell_price)).rstrip('0').rstrip('.')
                self.logger.info(f'[{pair}] (UNSUCCESSFUL TRADE) Try sell @ {krk_sell_price}')
                result = krk_sell_price
                break
            except:
                self.logger.info(traceback.format_exc())
                time.sleep(8)
                continue
        return result

    async def get_kraken_exchange_status(self):
        result = 'online'
        for _ in range (5):
            try:
                krk_status = await self.krk.query_public("SystemStatus")
                if krk_status['error'] != []:
                    raise
                result = krk_status['result']['status']
                break
            except:
                self.logger.info(traceback.format_exc())
                time.sleep(8)
                continue
        return result
