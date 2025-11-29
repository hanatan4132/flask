from flask import Flask, render_template, jsonify
import ccxt
import time
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# --- 1. 初始化 Flask APP (Gunicorn 就是在找這一行) ---
app = Flask(__name__)

# --- 全局配置 ---
CACHE_DURATION = 60
EXCHANGES = ['binance', 'bybit', 'bitget']

# 全局緩存
cache_data = {
    "timestamp": 0,
    "rates": []
}

def format_time(timestamp):
    """將毫秒時間戳轉換為 UTC+8 的 H:M:S"""
    if timestamp:
        try:
            # 1. 先轉成標準 UTC 時間
            dt_utc = datetime.utcfromtimestamp(float(timestamp) / 1000)
            # 2. 加上 8 小時 (台灣時間)
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
            'timeout': 10000,
        }

        exchange_class = getattr(ccxt, exchange_id)
        config = {**common_config}
        
        # 交易所特定設定
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
            
            # --- 獲取數據邏輯 ---
            try:
                if exchange.has['fetchFundingRates']:
                    rates = exchange.fetch_funding_rates()
                else:
                    raise Exception("Method not supported")
            except Exception:
                # 備案: 如果 fetch_funding_rates 失敗才用 Tickers
                tickers = exchange.fetch_tickers()
                for symbol, ticker in tickers.items():
                    if 'fundingRate' in ticker:
                         rates[symbol] = {
                             'fundingRate': ticker['fundingRate'], 
                             'fundingTimestamp': ticker.get('nextFundingTime') or ticker.get('fundingTimestamp')
                         }

            # --- 預先計算 Bitget 的下次結算時間 (數學補位法) ---
            # Bitget 規則: 每 8 小時一次 (00, 08, 16 UTC)
            bitget_calc_timestamp = None
            if exchange_id == 'bitget':
                now_ms = time.time() * 1000
                eight_hours_ms = 8 * 60 * 60 * 1000
                # 算出下一個 8 小時節點
                bitget_calc_timestamp = (int(now_ms) // eight_hours_ms + 1) * eight_hours_ms

            # --- 統一整理數據 ---
            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                
                if is_usdt:
                    rate = info.get('fundingRate')
                    
                    # --- 時間戳處理邏輯 (多重偵測) ---
                    next_time = info.get('fundingTimestamp')
                    if next_time is None:
                        next_time = info.get('nextFundingTime')
                    if next_time is None:
                        next_time = info.get('fundingTime')
                    
                    # 特殊處理: 如果是 Bitget 且 API 沒給時間，用算的
                    if exchange_id == 'bitget' and next_time is None:
                        next_time = bitget_calc_timestamp

                    if rate is not None:
                        raw_data.append({
                            'exchange': exchange_id,
                            'symbol': symbol.replace(':USDT', '/USDT'),
                            'rate': float(rate),
                            'next_time_formatted': format_time(next_time) # 這裡會自動轉 UTC+8
                        })

    except Exception as e:
        print(f"Error fetching {exchange_id}: {str(e)}")
    
    return raw_data

def get_aggregated_rates():
    """獲取並聚合數據"""
    global cache_data
    current_time = time.time()

    if current_time - cache_data['timestamp'] < CACHE_DURATION and cache_data['rates']:
        return cache_data['rates'], False

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching new data from exchanges...")
    
    all_rates = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(fetch_exchange_rates, EXCHANGES)
        for res in results:
            all_rates.extend(res)

    # 聚合數據結構，讓前端可以做橫向排列
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
    
    # 預設排序：優先以 Binance 資費排序 (空值排最後)
    final_list.sort(key=lambda x: x.get('binance_rate') if x.get('binance_rate') is not None else float('inf'))

    if final_list:
        cache_data['rates'] = final_list
        cache_data['timestamp'] = current_time
    
    return final_list, True

@app.route('/')
def index():
    return render_template('index.html', exchanges=EXCHANGES)

@app.route('/api/rates')
def api_rates():
    try:
        data, updated = get_aggregated_rates()
        return jsonify({
            'updated_at': datetime.fromtimestamp(cache_data['timestamp']).strftime('%H:%M:%S'),
            'count': len(data),
            'data': data
        })
    except Exception as e:
        print(f"API Error: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
