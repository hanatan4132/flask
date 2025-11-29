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
            
            # --- 獲取數據 (全改回 fetch_funding_rates 以確保有數據) ---
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

            # --- 統一整理數據 ---
            # 預先計算 Bitget 的下次結算時間 (以防 API 沒給)
            # Bitget 規則: 每 8 小時一次 (00, 08, 16 UTC)
            # 算法: (當前時間戳 // 8小時毫秒數 + 1) * 8小時毫秒數
            bitget_calc_timestamp = None
            if exchange_id == 'bitget':
                now_ms = time.time() * 1000
                eight_hours_ms = 8 * 60 * 60 * 1000
                bitget_calc_timestamp = (int(now_ms) // eight_hours_ms + 1) * eight_hours_ms

            for symbol, info in rates.items():
                is_usdt = '/USDT' in symbol or ':USDT' in symbol
                
                if is_usdt:
                    rate = info.get('fundingRate')
                    
                    # --- 時間戳處理邏輯 ---
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
                            # 這裡 format_time 會自動幫你 +8 小時
                            'next_time_formatted': format_time(next_time) 
                        })

    except Exception as e:
        print(f"Error fetching {exchange_id}: {str(e)}")
    
    return raw_data
