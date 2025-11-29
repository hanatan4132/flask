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

CACHE_DURATION = 180  # 緩存 60 秒
def format_time(timestamp):
    """將毫秒時間戳轉換為 H:M:S 格式 (不顯示日期)"""
    if timestamp:
        # 轉換為秒
        dt_object = datetime.fromtimestamp(timestamp / 1000)
        # 只顯示小時:分鐘:秒
        return dt_object.strftime('%H:%M:%S') 
    return '-'
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
                    
                    next_time = info.get('nextFundingTime')
                    
                    if rate is not None:
                        raw_data.append({
                            'exchange': exchange_id,
                            'symbol': symbol.replace(':USDT', '/USDT'), # 統一格式
                            'rate': float(rate),
                            'rate_pct': round(float(rate) * 100, 4),
                            'next_time_raw': next_time,
                            'next_time_formatted': format_time(next_time)
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

    aggregated_data = {}
    
    # 1. 聚合: 將所有交易所的數據依 Symbol 歸類
    for item in all_rates:
        symbol = item['symbol']
        exchange = item['exchange']
        
        if symbol not in aggregated_data:
            # 初始化這個幣種的聚合結構
            aggregated_data[symbol] = {'symbol': symbol}
            for ex in EXCHANGES:
                # 初始化每個交易所的數據為 None 或 '-'
                aggregated_data[symbol][f'{ex}_rate'] = None
                aggregated_data[symbol][f'{ex}_time'] = '-'
        
        # 填充數據
        aggregated_data[symbol][f'{exchange}_rate'] = item['rate']
        aggregated_data[symbol][f'{exchange}_time'] = item['next_time_formatted']
        
    # 2. 排序: 根據幣安 (或任一交易所) 的費率由小到大排序
    final_list = list(aggregated_data.values())
    
    # 排序邏輯：優先以幣安費率排序，如果幣安沒有數據，就將該幣種排在後面
    def sort_key(item):
        rate = item.get('binance_rate')
        return rate if rate is not None else float('inf')

    sorted_list = sorted(final_list, key=sort_key)


    if sorted_list:
        cache_data['rates'] = sorted_list
        cache_data['timestamp'] = current_time
    
    return sorted_list, True

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
