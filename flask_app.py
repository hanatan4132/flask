from flask import Flask, render_template, jsonify
import ccxt
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# 全局緩存變量
cache_data = {
    "timestamp": 0,
    "rates": []
}

# 設定緩存過期時間 (秒)
CACHE_DURATION = 60

def fetch_exchange_rates(exchange_id):
    """抓取單一交易所的資費"""
    data = []
    try:
        # 初始化交易所
        exchange_class = getattr(ccxt, exchange_id)
        # 設定為合約模式 (Swap/Future)
        options = {}
        if exchange_id == 'binance':
            options = {'defaultType': 'future'}
        elif exchange_id == 'bybit':
            options = {'defaultType': 'swap'} # Bybit linear swap
        elif exchange_id == 'bitget':
            options = {'defaultType': 'swap'}

        exchange = exchange_class({
            'enableRateLimit': True,
            'options': options
        })

        # 獲取所有資費
        # ccxt 的 fetch_funding_rates 通常會回傳該交易所所有支持的幣種
        rates = exchange.fetch_funding_rates()
        
        for symbol, info in rates.items():
            # 過濾條件：只抓 USDT 結算的合約
            if '/USDT' in symbol: 
                rate = info.get('fundingRate')
                timestamp = info.get('timestamp')
                
                if rate is not None:
                    data.append({
                        'exchange': exchange_id.capitalize(),
                        'symbol': symbol,
                        'rate': rate,
                        'rate_pct': round(rate * 100, 4), # 轉為百分比
                        'time': datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S') if timestamp else '-'
                    })
    except Exception as e:
        print(f"Error fetching {exchange_id}: {e}")
    
    return data

def get_sorted_rates():
    """獲取並排序所有交易所數據 (含緩存機制)"""
    global cache_data
    current_time = time.time()

    # 如果緩存還很新，直接回傳緩存
    if current_time - cache_data['timestamp'] < CACHE_DURATION and cache_data['rates']:
        return cache_data['rates'], False # False 代表沒有更新

    print("Fetching new data from exchanges...")
    all_rates = []
    exchanges = ['binance', 'bybit', 'bitget']

    # 使用線程池並行抓取，加快速度
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = executor.map(fetch_exchange_rates, exchanges)
        for res in results:
            all_rates.extend(res)

    # 排序：由小到大
    # x['rate'] 可能是 float，要確保排序正確
    sorted_rates = sorted(all_rates, key=lambda x: x['rate'])

    # 更新緩存
    cache_data['rates'] = sorted_rates
    cache_data['timestamp'] = current_time
    
    return sorted_rates, True

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/rates')
def api_rates():
    """前端透過 AJAX 請求這個 API 獲取數據"""
    data, updated = get_sorted_rates()
    return jsonify({
        'updated_at': datetime.fromtimestamp(cache_data['timestamp']).strftime('%H:%M:%S'),
        'data': data
    })

# 本地測試用
if __name__ == '__main__':
    app.run(debug=True)
