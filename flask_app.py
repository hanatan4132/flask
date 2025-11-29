from flask import Flask, render_template, jsonify
import ccxt
import time
import os  # 新增: 用於讀取環境變數
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
    exchange = None
    try:
        # 動態獲取交易所類別
        exchange_class = getattr(ccxt, exchange_id)
        
        # --- 這裡定義 common_config ---
        common_config = {
            'enableRateLimit': True,
            'timeout': 10000,  # 10秒超時
        }

        # 針對不同交易所的特定設定
        if exchange_id == 'binance':
            # 嘗試從 Render 環境變數讀取 API Key
            api_key = os.environ.get('BINANCE_API_KEY')
            secret = os.environ.get('BINANCE_SECRET')
            
            # 基礎設定
            config = {
                **common_config, 
                'options': {'defaultType': 'future'}
            }
            
            # 如果有設定 API Key，就加進去 (解決 418 IP Ban 問題)
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

        # 嘗試載入市場
        exchange.load_markets()

        # --- 獲取資費邏輯 ---
        rates = {}
        
        try:
            if exchange.has['fetchFundingRates']:
                rates = exchange.fetch_funding_rates()
            else:
                raise Exception("Method not supported")
        except Exception:
            # 備案: 從 Tickers 獲取
            print(f"Fallback: Fetching tickers for {exchange_id}")
            tickers = exchange.fetch_tickers()
            for symbol, ticker in tickers.items():
                if 'fundingRate' in ticker and ticker['fundingRate'] is not None:
                    rates[symbol] = {
                        'symbol': symbol,
                        'fundingRate': ticker['fundingRate'],
                        'timestamp': ticker['timestamp']
                    }

        # 處理數據
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
        print(f"Error fetching {exchange_id}: {error_msg}")

    finally:
        if exchange:
            exchange.close()
    
    return data

def get_sorted_rates():
    global cache_data
    current_time = time.time()

    if current_time - cache_data['timestamp'] < CACHE_DURATION and cache_data['rates']:
        return cache_data['rates'], False

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching new data from exchanges...")
    all_rates = []
    
    # Render 環境通常可以抓到這三家 (只要 Binance 加上 API Key)
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
    data, updated = get_sorted_rates()
    return jsonify({
        'updated_at': datetime.fromtimestamp(cache_data['timestamp']).strftime('%H:%M:%S'),
        'count': len(data),
        'data': data
    })

if __name__ == '__main__':
    # 這裡的 PORT 是給本地測試用的，Render 會使用 gunicorn 指令覆蓋這裡
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
