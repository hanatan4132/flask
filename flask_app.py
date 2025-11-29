from flask import Flask, render_template, jsonify
import ccxt
import time
import os
from datetime import datetime, timedelta # <--- 1. 新增 timedelta
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 全局配置 ---
CACHE_DURATION = 180
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
            
            # --- 針對 Bitget 的特殊處理 (解決時間顯示問題) ---
            if exchange_id == 'bitget':
                try:
                    # Bitget 使用 fetch_tickers 比較容易拿到原始的 nextUpdate
                    tickers = exchange.fetch_tickers()
                    for symbol, ticker in tickers.items():
                        if 'fundingRate' in ticker:
                            # 嘗試從原始數據 info 中獲取 nextUpdate
                            # 根據你的測試，欄位是 'nextUpdate'
                            next_time = ticker.get('info', {}).get('nextUpdate')
                            
                            rates[symbol] = {
                                'fundingRate': ticker['fundingRate'],
                                'fundingTimestamp': next_time # 直接用原始數據
                            }
                except Exception as e:
                    print(f"Bitget fetch error: {e}")

            # --- 其他交易所 (Binance / Bybit) 使用標準方法 ---
            else:
                try:
                    if exchange.has['fetchFundingRates']:
                        rates = exchange.fetch_funding_rates()
                    else:
                        raise Exception("Method not supported")
                except Exception:
                    # 備案
                    tickers = exchange.fetch_tickers()
                    for symbol, ticker in tickers.items():
                        if 'fundingRate' in ticker:
                             rates[symbol] = {
                                 'fundingRate': ticker['fundingRate'], 
                                 'fundingTimestamp': ticker.get('nextFundingTime') or ticker.get('fundingTimestamp')
                             }

            # --- 統一整理數據 ---
            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                
                if is_usdt:
                    rate = info.get('fundingRate')
                    
                    # 多重欄位偵測 (相容各交易所)
                    next_time = info.get('fundingTimestamp')
                    if next_time is None:
                        next_time = info.get('nextFundingTime')
                    if next_time is None:
                        next_time = info.get('fundingTime')

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

    # 聚合
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
    
    # 簡單預設排序
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
