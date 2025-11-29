from flask import Flask, render_template, jsonify
import ccxt
import time
import os
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# 全局緩存
cache_data = {
    "timestamp": 0,
    "rates": []
}

CACHE_DURATION = 60  # 緩存 60 秒

def fetch_exchange_rates(exchange_id):
    data = []
    # 初始化 exchange 為 None
    exchange = None 
    
    try:
        # 1. 先定義通用設定 (解決 NameError)
        common_config = {
            'enableRateLimit': True,
            'timeout': 10000,  # 10秒超時
        }

        # 動態獲取交易所類別
        exchange_class = getattr(ccxt, exchange_id)
        
        # 2. 針對不同交易所的特定設定
        if exchange_id == 'binance':
            # 嘗試從環境變數讀取 API Key
            api_key = os.environ.get('BINANCE_API_KEY')
            secret = os.environ.get('BINANCE_SECRET')
            
            config = {
                **common_config, 
                'options': {'defaultType': 'future'}
            }
            
            # 如果有 Key，就加入設定
            if api_key and secret:
                config['apiKey'] = api_key
                config['secret'] = secret
            
            exchange = exchange_class(config)

        elif exchange_id == 'bybit':
            # Bybit V5 API
            exchange = exchange_class({**common_config, 'options': {'defaultType': 'swap'}})
            
        elif exchange_id == 'bitget':
            # Bitget 混合合約
            exchange = exchange_class({**common_config, 'options': {'defaultType': 'swap'}})

        # 3. 嘗試載入市場
        if exchange:
            exchange.load_markets()

            # --- 獲取資費邏輯 ---
            rates = {}
            
            try:
                # 優先嘗試 fetch_funding_rates
                if exchange.has['fetchFundingRates']:
                    rates = exchange.fetch_funding_rates()
                else:
                    raise Exception("Method not supported")
            except Exception:
                # 備案: 從 Tickers 獲取
                # print(f"Fallback: Fetching tickers for {exchange_id}") # 減少 log 雜訊
                tickers = exchange.fetch_tickers()
                for symbol, ticker in tickers.items():
                    if 'fundingRate' in ticker and ticker['fundingRate'] is not None:
                        rates[symbol] = {
                            'symbol': symbol,
                            'fundingRate': ticker['fundingRate'],
                            'timestamp': ticker['timestamp']
                        }

            # 4. 處理數據
            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                
                if is_usdt:
                    rate = info.get('fundingRate')
                    timestamp = info.get('timestamp')
                    
                    if rate is not None:
                        data.append({
                            'exchange': exchange_id.capitalize(),
                            'symbol': symbol,
                            'rate': float(rate),
                            'rate_pct': round(float(rate) * 100, 4),
                            'time': datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S') if timestamp else '-'
                        })

    except Exception as e:
        error_msg = str(e)
        # 只印出簡短錯誤，避免 Log 爆炸
        print(f"Error fetching {exchange_id}: {error_msg}")

    # 注意：同步版 CCXT 不需要 finally exchange.close()
    
    return data

def get_sorted_rates():
    global cache_data
    current_time = time.time()

    if current_time - cache_data['timestamp'] < CACHE_DURATION and cache_data['rates']:
        return cache_data['rates'], False

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching new data from exchanges...")
    all_rates = []
    exchanges = ['binance', 'bybit', 'bitget']

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(fetch_exchange_rates, exchanges)
        for res in results:
            all_rates.extend(res)

    sorted_rates = sorted(all_rates, key=lambda x: x['rate'])

    if sorted_rates:
        cache_data['rates'] = sorted_rates
        cache_data['timestamp'] = current_time
    
    return sorted_rates, True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/rates')
def api_rates():
    try:
        data, updated = get_sorted_rates()
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
