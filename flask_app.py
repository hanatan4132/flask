from flask import Flask, render_template, jsonify
import ccxt
import time
import os
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 全局配置 ---
# 注意：這裡的 CACHE_DURATION 變成後台更新的間隔時間
UPDATE_INTERVAL = 180 
EXCHANGES = ['binance', 'bybit', 'bitget']

# 全局數據容器 (這是我們的"現成菜餚")
global_data_store = {
    "timestamp": 0,
    "rates": [],
    "is_updating": False, # 標記是否正在更新中
    "last_success": None  # 上次成功更新的時間文字
}

def format_time(timestamp):
    """將毫秒時間戳轉換為 UTC+8 的 H:M:S"""
    if timestamp:
        try:
            dt_utc = datetime.utcfromtimestamp(float(timestamp) / 1000)
            dt_tw = dt_utc + timedelta(hours=8)
            return dt_tw.strftime('%H:%M:%S')
        except:
            return '-'
    return '-'

def fetch_exchange_rates(exchange_id):
    """抓取單一交易所數據"""
    raw_data = []
    exchange = None
    
    try:
        common_config = {
            'enableRateLimit': True,
            'timeout': 20000, # 放寬超時時間，因為後台跑沒人等
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
            except Exception:
                tickers = exchange.fetch_tickers()
                for symbol, ticker in tickers.items():
                    if 'fundingRate' in ticker:
                         rates[symbol] = {
                             'fundingRate': ticker['fundingRate'], 
                             'fundingTimestamp': ticker.get('nextFundingTime') or ticker.get('fundingTimestamp')
                         }

            # Bitget 數學補位法
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
        print(f"Error fetching {exchange_id}: {str(e)}")
    
    return raw_data

def update_data_task():
    """這是後台任務，會一直在迴圈中執行"""
    print("啟動後台更新線程...")
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始後台更新數據...")
            global_data_store['is_updating'] = True
            
            all_rates = []
            # 使用線程池並行抓取
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = executor.map(fetch_exchange_rates, EXCHANGES)
                for res in results:
                    all_rates.extend(res)

            # 聚合數據
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
            # 預設排序
            final_list.sort(key=lambda x: x.get('binance_rate') if x.get('binance_rate') is not None else float('inf'))

            # 更新全局變數 (這是原子操作，對於讀取來說是安全的)
            if final_list:
                global_data_store['rates'] = final_list
                global_data_store['timestamp'] = time.time()
                global_data_store['last_success'] = datetime.now().strftime('%H:%M:%S')
                print(f"數據更新完成，共 {len(final_list)} 筆。")
            
        except Exception as e:
            print(f"後台更新失敗: {e}")
        finally:
            global_data_store['is_updating'] = False
            
        # 休息 60 秒再做下一次
        time.sleep(UPDATE_INTERVAL)

# --- 啟動後台線程 ---
# 使用 daemon=True，這樣當主程式結束時，線程也會跟著結束
bg_thread = threading.Thread(target=update_data_task, daemon=True)
bg_thread.start()

@app.route('/')
def index():
    return render_template('index.html', exchanges=EXCHANGES)

@app.route('/api/rates')
def api_rates():
    # 使用者請求時，直接回傳記憶體裡的數據，不做任何運算
    # 這是真正的 "Instant" 響應
    
    response_data = {
        'count': len(global_data_store['rates']),
        'data': global_data_store['rates'],
        'updated_at': global_data_store['last_success'] or "初始載入中..."
    }
    
    # 如果是剛啟動，數據還是空的，可能正在抓
    if not global_data_store['rates']:
        response_data['status'] = 'initializing'
    
    return jsonify(response_data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
