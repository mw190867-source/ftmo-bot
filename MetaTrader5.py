from mt5linux import MetaTrader5
_mt5 = MetaTrader5(host="localhost", port=18812)

def initialize(*args, **kwargs):
    return _mt5.initialize(*args, **kwargs)

def shutdown():
    return _mt5.shutdown()

def account_info():
    return _mt5.account_info()

def positions_get(**kwargs):
    return _mt5.positions_get(**kwargs)

def orders_get(**kwargs):
    return _mt5.orders_get(**kwargs)

def order_send(request):
    return _mt5.order_send(request)

def copy_rates_from_pos(symbol, timeframe, start_pos, count):
    return _mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)

def copy_rates_range(symbol, timeframe, date_from, date_to):
    return _mt5.copy_rates_range(symbol, timeframe, date_from, date_to)

def symbol_info(symbol):
    return _mt5.symbol_info(symbol)

def symbol_info_tick(symbol):
    return _mt5.symbol_info_tick(symbol)

def last_error():
    return _mt5.last_error()

TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
TIMEFRAME_MN1 = 49153

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6

def version():
    return _mt5.version()

def terminal_info():
    return _mt5.terminal_info()

def order_check(request):
    return _mt5.order_check(request)

def history_orders_get(*args, **kwargs):
    return _mt5.history_orders_get(*args, **kwargs)

def history_deals_get(*args, **kwargs):
    return _mt5.history_deals_get(*args, **kwargs)

def symbol_select(symbol, enable=True):
    return _mt5.symbol_select(symbol, enable)

def market_book_add(symbol):
    return _mt5.market_book_add(symbol)

def market_book_get(symbol):
    return _mt5.market_book_get(symbol)

def order_calc_profit(action, symbol, volume, price_open, price_close):
    return _mt5.order_calc_profit(action, symbol, volume, price_open, price_close)

def order_calc_margin(action, symbol, volume, price):
    return _mt5.order_calc_margin(action, symbol, volume, price)

ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
TRADE_ACTION_MODIFY = 7
TRADE_RETCODE_DONE = 10009

ORDER_TIME_GTC = 1
ORDER_TIME_DAY = 0
ORDER_TIME_SPECIFIED = 2
ORDER_TIME_SPECIFIED_DAY = 3
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_CANCEL = 10007
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_ERROR = 10011
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_INVALID_EXPIRATION = 10022
TRADE_RETCODE_ORDER_CHANGED = 10023
TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
TRADE_RETCODE_CONNECTION = 10045
