import time
import traceback
import asyncio
from random import sample
from yatbkrk_constants import *

class Simulator:
    """Configures Simulator object to simulate buy/sell actions"""
    def __init__(self, krk_exchange, google_sheets_helper, logger, telegram):
        self.krk_exchange = krk_exchange
        self.google_sheets_helper = google_sheets_helper
        self.logger = logger
        self.telegram = telegram

    def get_krk_fees(self, volume):
        return FEES[list(filter(lambda item: float(item) <= float(volume), FEES))[-1]]

    async def simulate_limit_buy_sell(self, pair, krk_pair, trade_amount, interval, trades):
        result = 0
        # Get bid/ask prices
        for _ in range(20):
            try:
                await asyncio.sleep(sample(NONCE,1)[0])
                krk_asset_pair = await self.krk_exchange.query_public("AssetPairs", {'pair': pair})
                if krk_asset_pair['error'] != []:
                    raise
                pair_decimals = krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['pair_decimals']
                self.logger.info(f'[{pair}] Pair Decimals {str(pair_decimals)}')
                lot_decimals = krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['lot_decimals']
                self.logger.info(f'[{pair}] Lot Decimals {str(lot_decimals)}')
                # order_min = len(krk_asset_pair['result'][list(krk_asset_pair['result'].keys())[0]]['ordermin'].split(".")[1])
                # self.logger.info(f'[{pair}] Order Min Decimals {str(order_min)}')
                krk_tickers = await self.krk_exchange.query_public("Ticker", {'pair': pair})
                if krk_tickers['error'] != []:
                    raise
                krk_tickers_result = krk_tickers['result'][list(krk_tickers['result'].keys())[0]]
                krk_buy_price = krk_tickers_result['b'][0]
                self.logger.info(f'[{pair}] ({interval}) Buy price {krk_buy_price}')
                krk_sell_price = krk_tickers_result['a'][0]
                self.logger.info(f'[{pair}] ({interval}) Sell price {krk_sell_price}')
                self.logger.info(f'[{pair}] ({interval}) Sell/Buy = {round(float(krk_sell_price) / float(krk_buy_price), 4)}')
                # krk_try_buy_price = float(krk_buy_price) * sample(PRICE_REDUCTOR,1)[0]
                decimal_formatter = "%." + str(pair_decimals) + "f"
                # krk_try_buy_price = (decimal_formatter % krk_try_buy_price).rstrip('0').rstrip('.')
                krk_buy_price = (decimal_formatter % float(krk_buy_price)).rstrip('0').rstrip('.')
                self.logger.info(f'[{pair}] ({interval}) Try buy @ {krk_buy_price}')
                krk_buy_base = float(trade_amount) / float(krk_buy_price)
                lot_decimal_formatter = "%." + str(lot_decimals) + "f"
                krk_buy_base = (lot_decimal_formatter % krk_buy_base).rstrip('0').rstrip('.')
                self.logger.info(f'[{pair}] ({interval}) Try buy {krk_buy_base} with ${trade_amount}')
                # expected_profit_percentage = 1.001
                krk_sell_price = float(krk_buy_price) * SELL_PERCENTAGE[interval]
                krk_sell_price = (decimal_formatter % krk_sell_price).rstrip('0').rstrip('.')
                self.logger.info(f'[{pair}] ({interval}) Try sell @ {krk_sell_price}')
                result = True
                break
            except:
                self.logger.info(traceback.format_exc())
                time.sleep(8)
                continue
        if result:
            # krk_tickers_result = krk_tickers['result'][krk_pair]
            # krk_buy_price = krk_tickers_result['b'][0]
            # Simulate buy and sell orders
            account_30d_volume = round(float(self.google_sheets_helper.get_cell_value('SimulationTest!T2:T2')), 2)
            fees = self.get_krk_fees(account_30d_volume)
            quantity = round((float(trade_amount) / float(krk_buy_price)) * (1.0 - fees), 5)
            profit = round((float(krk_sell_price) * quantity * (1.0 - fees)) - trade_amount, 3)
            expiry_time = TRADE_EXPIRY_TIME[interval]
            trades.append({'pair': pair, 'krk_pair': krk_pair, 'type': 'sim', 'interval': interval, 'status': 'active', 'orderid': 0, 'time': time.time(), 'expirytime': expiry_time, 'buyprice': krk_buy_price, 'expsellprice': krk_sell_price, 'quantity': quantity, 'profit': profit, 'trade_amount': float(trade_amount)})
            result = float(trade_amount)
            # self.google_sheets_helper.update_row('SimulationTest!T2:T2', account_30d_volume)
            log_message = f"<YATB SIM> [{pair}] Bought {str(trade_amount)} @ {krk_buy_price} = {str(quantity)}. Sell @ {str(krk_sell_price)}. Exp. Profit ~ {str(profit)} USD"
            self.logger.info(log_message)
            if self.telegram.notifications_on:
                self.telegram.send(log_message)
        return result
