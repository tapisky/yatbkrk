import time
import traceback

FEES = {'0': 0.0016, '49999': 0.0014, '99999': 0.0012, '249999': 0.001, '499999': 0.0008, '999999': 0.0006,
        '2499999': 0.0004, '4999999': 0.0002, '9999999': 0.0}

class Simulator:
    def __init__(self, krk_exchange, logger, telegram):
        self.krk_exchange = krk_exchange
        self.logger = logger
        self.telegram = telegram

    def get_krk_fees(self, volume):
        return FEES[list(filter(lambda item: float(item) <= float(volume), FEES))[-1]]

    async def simulate_limit_buy_sell(self, pair, krk_pair, trade_amount, account_30d_volume, trades):
        result = False
        # Get bid price
        for _ in range(20):
            try:
                krk_tickers = await self.krk_exchange.query_public("Ticker", {'pair': pair})
                break
            except:
                self.logger.info(traceback.format_exc())
                time.sleep(8)
                continue
        if krk_tickers['error'] != []:
            raise
        krk_tickers_result = krk_tickers['result'][krk_pair]
        krk_buy_price = krk_tickers_result['b'][0]
        expected_profit_percentage = 1.001
        # Simulate buy ans sell orders
        quantity = round((float(trade_amount) / float(krk_buy_price)) * (1.0 - self.get_krk_fees(account_30d_volume)), 5)
        expected_sell_price = round((float(quantity) * float(krk_buy_price) * expected_profit_percentage), 3)
        profit = round(expected_sell_price - trade_amount, 3)
        trades.append({'pair': pair, 'type': 'sim', 'interval': '15m', 'status': 'active', 'orderid': 0, 'time': time.time(), 'expirytime': time.time() + 43200.0, 'buyprice': float(krk_buy_price), 'expsellprice': expected_sell_price, 'quantity': quantity})
        account_30d_volume += float(trade_amount)
        log_message = f"<YATB SIM> [{pair}] Bought {str(trade_amount)} @ {krk_buy_price} = {str(quantity)}. Sell @ {str(expected_sell_price)}. Exp. Profit ~ {str(profit)} USD"
        self.logger.info(log_message)
        if self.telegram.notifications_on:
            self.telegram.send(log_message)
        result = True
        return result
