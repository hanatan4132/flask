from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# 設定日誌，讓我們在 Render 後台看得到錯誤
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- 全局配置 ---
UPDATE_INTERVAL = 150

EXCHANGES = ['binance', 'bybit', 'bitget']

# 全局數據容器
global_data_store = {
    "timestamp": 0,
    "rates": [],
    "status": "initializing", # 狀態: initializing, updated, error
    "last_success": None,
    "error_msg": None
}

# 用來控制後台執行緒的變數
bg_thread = None
thread_lock = threading.Lock()

def format_time(timestamp):
    if timestamp:
        try:
            dt_utc = datetime.utcfromtimestamp(float(timestamp) / 1000)
            dt_tw = dt_utc + timedelta(hours=8)
            return dt_tw.strftime('%H:%M:%S')
        except:
            return '-'
    return '-'

def fetch_exchange_rates(exchange_id):
    raw_data = []
    try:
        common_config = {
            'enableRateLimit': True,
            'timeout': 20000, 
        }

        exchange_class = getattr(ccxt, exchange_id)
        config = {**common_config}
        
        if exchange_id == 'binance':
            api_key = os.environ.get('BINANCE_API_KEY')
            secret = os.environ.get('BINANCE_SECRET')
            config = {**common_config, 'options': {'defaultType': 'future'}}
            if api_key and secret:
                config['apiKey'] = api_key
                config['secret'] = secret
            exchange = exchange_class(config)
        elif exchange_id == 'bybit':
            exchange = exchange_class({**common_config, 'options': {'defaultType': 'swap'}})
        elif exchange_id == 'bitget':
            exchange = exchange_class({**common_config, 'options': {'defaultType': 'swap'}})

        if exchange:
            exchange.load_markets()
            rates = {}
            
            try:
                if exchange.has['fetchFundingRates']:
                    rates = exchange.fetch_funding_rates()
                else:
                    raise Exception("Method not supported")
            except Exception as e:
                # logger.warning(f"{exchange_id} 批量獲取失敗，嘗試 Tickers: {e}")
                tickers = exchange.fetch_tickers()
                for symbol, ticker in tickers.items():
                    if 'fundingRate' in ticker:
                         rates[symbol] = {
                             'fundingRate': ticker['fundingRate'], 
                             'fundingTimestamp': ticker.get('nextFundingTime') or ticker.get('fundingTimestamp')
                         }

            bitget_calc_timestamp = None
            if exchange_id == 'bitget':
                now_ms = time.time() * 1000
                eight_hours_ms = 8 * 60 * 60 * 1000
                bitget_calc_timestamp = (int(now_ms) // eight_hours_ms + 1) * eight_hours_ms

            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                if is_usdt:
                    rate = info.get('fundingRate')
                    next_time = info.get('fundingTimestamp') or info.get('nextFundingTime') or info.get('fundingTime')
                    
                    if exchange_id == 'bitget' and next_time is None:
                        next_time = bitget_calc_timestamp

                    if rate is not None:
                        raw_data.append({
                            'exchange': exchange_id,
                            'symbol': symbol.replace(':USDT', '/USDT'),
                            'rate': float(rate),
                            'next_time_formatted': format_time(next_time)
                        })
    except Exception as e:
        logger.error(f"抓取 {exchange_id} 時發生錯誤: {e}")
    
    return raw_data

def update_data_task():
    """後台任務：持續抓取數據"""
    logger.info("--- 後台更新線程已啟動 ---")
    while True:
        try:
            logger.info("開始執行新一輪抓取...")
            all_rates = []
            
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = executor.map(fetch_exchange_rates, EXCHANGES)
                for res in results:
                    all_rates.extend(res)

            # 聚合邏輯
            aggregated_data = {}
            for item in all_rates:
                symbol = item['symbol']
                exchange = item['exchange']
                
                if symbol not in aggregated_data:
                    aggregated_data[symbol] = {'symbol': symbol}
                    for ex in EXCHANGES:
                        aggregated_data[symbol][f'{ex}_rate'] = None
                        aggregated_data[symbol][f'{ex}_time'] = '-'
                
                aggregated_data[symbol][f'{exchange}_rate'] = item['rate']
                aggregated_data[symbol][f'{exchange}_time'] = item['next_time_formatted']
                
            final_list = list(aggregated_data.values())
            final_list.sort(key=lambda x: x.get('binance_rate') if x.get('binance_rate') is not None else float('inf'))

            # 無論是否抓到空值，都視為一次嘗試
            if final_list:
                global_data_store['rates'] = final_list
                global_data_store['timestamp'] = time.time()
                global_data_store['last_success'] = datetime.now().strftime('%H:%M:%S')
                global_data_store['status'] = 'updated'
                logger.info(f"更新成功: 抓到 {len(final_list)} 筆資料")
            else:
                # 如果抓回來是空的，可能是全部都失敗了
                global_data_store['status'] = 'empty'
                global_data_store['error_msg'] = "所有交易所皆無數據"
                logger.warning("更新完成但無數據")
            
        except Exception as e:
            logger.error(f"後台任務崩潰: {e}")
            global_data_store['status'] = 'error'
            global_data_store['error_msg'] = str(e)
        
        time.sleep(UPDATE_INTERVAL)

def start_background_thread():
    """安全地啟動後台線程"""
    global bg_thread
    with thread_lock:
        if bg_thread is None or not bg_thread.is_alive():
            bg_thread = threading.Thread(target=update_data_task, daemon=True)
            bg_thread.start()

@app.route('/')
def index():
    # 當有人訪問首頁時，檢查線程是否活著
    start_background_thread()
    return render_template('index.html', exchanges=EXCHANGES)

@app.route('/api/rates')
def api_rates():
    # 當 API 被呼叫時，也檢查線程是否活著 (雙保險)
    start_background_thread()
    
    response_data = {
        'count': len(global_data_store['rates']),
        'data': global_data_store['rates'],
        'updated_at': global_data_store['last_success'] or "初始載入中...",
        'status': global_data_store['status']
    }
    
    return jsonify(response_data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
