from flask import Flask, render_template, jsonify
import ccxt
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# --- 全局配置 ---
CACHE_DURATION = 180
EXCHANGES = ['binance', 'bybit', 'bitget']  # 定義交易所列表

# 全局緩存
cache_data = {
    "timestamp": 0,
    "rates": []
}

def format_time(timestamp):
    """將毫秒時間戳轉換為 H:M:S"""
    if timestamp:
        dt_object = datetime.fromtimestamp(timestamp / 1000)
        return dt_object.strftime('%H:%M:%S') 
    return '-'

def fetch_exchange_rates(exchange_id):
    """抓取單一交易所數據"""
    # 修正: 確保 raw_data 在 try 外部初始化
    raw_data = []
    exchange = None
    
    try:
        # 定義通用設定 (解決 name error)
        common_config = {
            'enableRateLimit': True,
            'timeout': 10000,
        }

        exchange_class = getattr(ccxt, exchange_id)
        config = {**common_config}
        
        # 針對交易所的特殊設定
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
            
            # 獲取資費
            try:
                if exchange.has['fetchFundingRates']:
                    rates = exchange.fetch_funding_rates()
                else:
                    raise Exception("Method not supported")
            except Exception:
                # 備案: 從 Tickers 獲取
                tickers = exchange.fetch_tickers()
                for symbol, ticker in tickers.items():
                    if 'fundingRate' in ticker and ticker['fundingRate'] is not None:
                         rates[symbol] = {'fundingRate': ticker['fundingRate'], 'nextFundingTime': None}

            # 處理數據
            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                
                if is_usdt:
                    rate = info.get('fundingRate')
                    next_time = info.get('nextFundingTime')
                    
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

def get_aggregated_rates():
    """獲取並聚合數據 (不負責排序，排序交給前端)"""
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
    
    # 預設排序 (可選，這裡預設先按幣安排序方便 API 查看)
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
