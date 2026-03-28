"""
fetchers.py
每個交易所一個 class，對外只暴露 fetch() -> list[dict]
dict 格式: {'symbol': 'BTC', 'rate': 0.0001, 'time': '08:00:00'}
"""

import time
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ─── 共用工具 ────────────────────────────────────────────────────────────────

def get_tw_time(ts_ms=None):
    if not ts_ms:
        return '-'
    try:
        dt_utc = datetime.fromtimestamp(int(ts_ms) / 1000, timezone.utc)
        dt_tw = dt_utc + timedelta(hours=8)
        return dt_tw.strftime('%H:%M:%S')
    except:
        return '-'

def strip_usdt(symbol: str) -> str:
    """BTC/USDT, BTCUSDT, BTC-USDT, BTC_USDT → BTC"""
    for sep in ['/', '-', '_']:
        if sep in symbol:
            base = symbol.split(sep)[0]
            return base
    # no separator — raw like BTCUSDT
    if symbol.endswith('USDT'):
        return symbol[:-4]
    return symbol

def safe_get(url, params=None, timeout=12, headers=None):
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"GET {url} failed: {e}")
        return None

def next_8h_ts_ms():
    """計算下一個整 8 小時 UTC 的毫秒時間戳（用於 Bitget 等沒回傳時間的）"""
    now = time.time() * 1000
    eight = 8 * 3600 * 1000
    return (int(now) // eight + 1) * eight


# ─── 各交易所 Fetcher ─────────────────────────────────────────────────────────

class BinanceFetcher:
    name = 'binance'

    def fetch(self):
        data = safe_get("https://fapi.binance.com/fapi/v1/premiumIndex")
        if not data:
            return []
        results = []
        for item in data:
            sym = item.get('symbol', '')
            if sym.endswith('USDT'):
                try:
                    results.append({
                        'symbol': strip_usdt(sym),
                        'rate': float(item['lastFundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    })
                except:
                    pass
        return results


class BybitFetcher:
    name = 'bybit'

    def fetch(self):
        data = safe_get("https://api.bybit.com/v5/market/tickers", params={'category': 'linear'})
        if not data or data.get('retCode') != 0:
            return []
        results = []
        for item in data['result']['list']:
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and item.get('fundingRate') not in (None, ''):
                try:
                    results.append({
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    })
                except:
                    pass
        return results


class BitgetFetcher:
    name = 'bitget'

    def fetch(self):
        data = safe_get("https://api.bitget.com/api/v2/mix/market/tickers",
                        params={'productType': 'USDT-FUTURES'})
        if not data or data.get('code') != '00000':
            return []
        calc_time = next_8h_ts_ms()
        results = []
        for item in data.get('data', []):
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and item.get('fundingRate') not in (None, ''):
                try:
                    results.append({
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(calc_time)
                    })
                except:
                    pass
        return results


class GateFetcher:
    name = 'gate'

    def fetch(self):
        data = safe_get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
        if not data:
            return []
        results = []
        for item in data:
            name = item.get('name', '')           # e.g. BTC_USDT
            if not name.endswith('_USDT'):
                continue
            rate = item.get('funding_rate')
            next_ts = item.get('funding_next_apply')  # unix seconds
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(name),
                    'rate': float(rate),
                    'time': get_tw_time(int(next_ts) * 1000 if next_ts else None)
                })
            except:
                pass
        return results


class OKXFetcher:
    name = 'okx'

    def fetch(self):
        # OKX 需要分頁，instType=SWAP 拿所有永續
        url = "https://www.okx.com/api/v5/public/funding-rate"
        # 先拿所有 USDT-SWAP 的 ticker 取得 instId 列表
        ticker_data = safe_get("https://www.okx.com/api/v5/market/tickers",
                               params={'instType': 'SWAP'})
        if not ticker_data or ticker_data.get('code') != '0':
            return []
        results = []
        inst_ids = [
            t['instId'] for t in ticker_data.get('data', [])
            if t['instId'].endswith('-USDT-SWAP')
        ]
        # OKX 沒有批量資費 endpoint，但 ticker 裡有 fundingRate（下次結算前的預估）
        # 改用 /api/v5/public/funding-rate?instId= 逐一查太慢
        # 使用 ticker 中的 fundingRate 欄位（v5 tickers 對 SWAP 有包含此欄）
        for t in ticker_data.get('data', []):
            inst_id = t.get('instId', '')
            if not inst_id.endswith('-USDT-SWAP'):
                continue
            # ticker 沒有 fundingRate，需另打 funding-rate endpoint
            # 但批量時先用已知欄位；若無則跳過
            pass

        # 正確做法：用 /public/funding-rate 但要一次一個 → 改為抓 mark-price-candles 批
        # 實際上 OKX ticker 對 SWAP 不含 fundingRate，需單獨查
        # 折中：用 open-interest endpoint 拼幣種，再並行查資費
        # 為效能考量，改用 /api/v5/market/tickers 加 /api/v5/public/funding-rate 批次抓
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def fetch_one(inst_id):
            d = safe_get(url, params={'instId': inst_id}, timeout=8)
            if d and d.get('code') == '0' and d.get('data'):
                item = d['data'][0]
                try:
                    next_ts = item.get('nextFundingTime')
                    return {
                        'symbol': inst_id.replace('-USDT-SWAP', ''),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(next_ts)
                    }
                except:
                    pass
            return None

        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, iid): iid for iid in inst_ids}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class MEXCFetcher:
    name = 'mexc'

    def fetch(self):
        data = safe_get("https://contract.mexc.com/api/v1/contract/funding_rate")
        if not data or data.get('success') is not True:
            return []
        results = []
        for item in data.get('data', []):
            sym = item.get('symbol', '')         # BTC_USDT
            if not sym.endswith('_USDT'):
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextSettleTime')  # ms
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class KuCoinFetcher:
    name = 'kucoin'

    def fetch(self):
        # KuCoin Futures 先拿所有合約清單
        contracts_data = safe_get("https://api-futures.kucoin.com/api/v1/contracts/active")
        if not contracts_data or contracts_data.get('code') != '200000':
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def fetch_one(symbol):
            d = safe_get(f"https://api-futures.kucoin.com/api/v1/funding-rate/{symbol}/current",
                         timeout=8)
            if d and d.get('code') == '200000' and d.get('data'):
                item = d['data']
                try:
                    return {
                        'symbol': symbol.replace('USDTM', ''),
                        'rate': float(item['value']),
                        'time': get_tw_time(item.get('timePoint'))
                    }
                except:
                    pass
            return None

        usdt_symbols = [
            c['symbol'] for c in contracts_data.get('data', [])
            if c.get('symbol', '').endswith('USDTM')
        ]
        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in usdt_symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class BingXFetcher:
    name = 'bingx'

    def fetch(self):
        # 先拿所有合約
        contracts = safe_get("https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
        if not contracts or contracts.get('code') != 0:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def fetch_one(sym):
            d = safe_get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 0 and d.get('data'):
                item = d['data']
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    }
                except:
                    pass
            return None

        usdt_syms = [
            c['symbol'] for c in contracts.get('data', [])
            if str(c.get('symbol', '')).endswith('-USDT')
        ]
        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in usdt_syms}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class XTFetcher:
    name = 'xt'

    def fetch(self):
        data = safe_get("https://fapi.xt.com/future/market/v1/public/q/funding-rate-list",
                        params={'pageSize': 500, 'page': 1})
        if not data or str(data.get('returnCode')) != '0':
            return []
        results = []
        for item in data.get('result', {}).get('items', []):
            sym = item.get('contractName', '')  # BTC_USDT
            if not sym.endswith('_USDT'):
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class LBankFetcher:
    name = 'lbank'

    def fetch(self):
        # LBank contract API 批量資費
        data = safe_get("https://www.lbkex.net/v2/contract/funding-rates.do",
                        params={'size': 200, 'current': 1})
        # fallback endpoint
        if not data or data.get('result') != 'true':
            data = safe_get("https://api.lbkex.com/contract/funding-rates.do",
                            params={'size': 200, 'current': 1})
        if not data or data.get('result') != 'true':
            return []
        results = []
        for item in data.get('data', {}).get('records', []):
            sym = item.get('contractId', '')   # BTC_USDT
            rate = item.get('currentRate')
            next_ts = item.get('nextFundingAt')
            if not sym.endswith('_USDT') or rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class WEEXFetcher:
    name = 'weex'

    def fetch(self):
        # WEEX V3 合約 API — 先取所有合約名稱，再批量查資費
        info = safe_get("https://api-contract.weex.com/capi/v3/market/contracts")
        if not info or info.get('code') != 200:
            # 改用 v2
            return self._fetch_v2()
        from concurrent.futures import ThreadPoolExecutor, as_completed

        symbols = [
            c['symbol'] for c in (info.get('data') or [])
            if str(c.get('symbol', '')).endswith('USDT')
        ]

        def fetch_one(sym):
            d = safe_get("https://api-contract.weex.com/capi/v3/market/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 200 and d.get('data'):
                item = d['data']
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    }
                except:
                    pass
            return None

        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results

    def _fetch_v2(self):
        data = safe_get("https://api-contract.weex.com/capi/v2/market/tickers")
        if not data or data.get('code') != 200:
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('fundingRate')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(item.get('nextFundingTime'))
                })
            except:
                pass
        return results


class ToobitFetcher:
    name = 'toobit'

    def fetch(self):
        # Toobit: GET /swap/v1/market/fundingRate  (需 symbol)
        # 先拿合約列表
        info = safe_get("https://api.toobit.com/api/v1/exchangeInfo")
        if not info:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        symbols = [
            s['contractName'] for s in info.get('contracts', [])
            if str(s.get('contractName', '')).endswith('USDT')
        ]

        def fetch_one(sym):
            d = safe_get("https://api.toobit.com/swap/v1/market/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('fundingRate') is not None:
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(d['fundingRate']),
                        'time': get_tw_time(d.get('nextFundingTime'))
                    }
                except:
                    pass
            return None

        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class BitunixFetcher:
    name = 'bitunix'

    def fetch(self):
        # Bitunix 有 batch endpoint: /api/v1/futures/market/funding_rate_batch (不需 symbol)
        data = safe_get("https://fapi.bitunix.com/api/v1/futures/market/batch_funding_rate")
        if not data or data.get('code') != 0:
            # fallback: 逐一查
            return self._fetch_one_by_one()
        results = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('fundingRate')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(item.get('nextFundingTime'))
                })
            except:
                pass
        return results

    def _fetch_one_by_one(self):
        pairs_data = safe_get("https://fapi.bitunix.com/api/v1/futures/market/get_trading_pairs")
        if not pairs_data or pairs_data.get('code') != 0:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        symbols = [
            p['symbol'] for p in (pairs_data.get('data') or [])
            if p.get('symbol', '').endswith('USDT')
        ]

        def fetch_one(sym):
            d = safe_get("https://fapi.bitunix.com/api/v1/futures/market/funding_rate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 0 and d.get('data'):
                item = d['data']
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    }
                except:
                    pass
            return None

        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class PhemexFetcher:
    name = 'phemex'

    def fetch(self):
        data = safe_get("https://api.phemex.com/md/v3/ticker/24hr/all",
                        params={'type': 'Perpetual'})
        if not data or data.get('code') != 0:
            return []
        results = []
        for item in (data.get('data', {}).get('tickers') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('fundingRateEr') or item.get('fundingRate')
            if rate is None:
                continue
            try:
                # Phemex 用 Ep 格式 (8 位小數)，fundingRateEr 需除 1e8
                rate_f = float(rate) / 1e8 if abs(float(rate)) > 1 else float(rate)
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': rate_f,
                    'time': get_tw_time(item.get('nextFundingTimeEp', 0) * 1000
                                        if item.get('nextFundingTimeEp') else None)
                })
            except:
                pass
        return results


class WhiteBITFetcher:
    name = 'whitebit'

    def fetch(self):
        data = safe_get("https://whitebit.com/api/v4/public/futures")
        if not data:
            return []
        results = []
        for sym, info in data.items():
            if not sym.endswith('_USDT') and not sym.endswith('/USDT'):
                continue
            rate = info.get('fundingRate') or info.get('funding_rate')
            next_ts = info.get('nextFundingTime') or info.get('next_funding_time')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class HTXFetcher:
    name = 'htx'

    def fetch(self):
        # HTX USDT-margined swap 批量資費
        data = safe_get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate")
        if not data or data.get('status') != 'ok':
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('contract_code', '')  # BTC-USDT
            if not sym.endswith('-USDT'):
                continue
            rate = item.get('funding_rate')
            next_ts = item.get('funding_time')   # ms str
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': sym.replace('-USDT', ''),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class PionexFetcher:
    name = 'pionex'

    def fetch(self):
        # Pionex /api/v1/market/perpetual/fundingRate  (公開，需 symbol)
        # 先拿合約列表
        info = safe_get("https://api.pionex.com/api/v1/market/tickers",
                        params={'type': 'PERP'})
        if not info or info.get('result') is not True:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        symbols = [
            t['symbol'] for t in (info.get('data', {}).get('tickers') or [])
            if str(t.get('symbol', '')).endswith('_USDT')
        ]

        def fetch_one(sym):
            d = safe_get("https://api.pionex.com/api/v1/market/perpetual/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('result') is True and d.get('data'):
                item = d['data']
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    }
                except:
                    pass
            return None

        results = []
        with ThreadPoolExecutor(max_workers=15) as ex:
            futures = {ex.submit(fetch_one, s): s for s in symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class DeepcoinFetcher:
    name = 'deepcoin'

    def fetch(self):
        data = safe_get("https://api.deepcoin.com/deepcoin/swap/fundingRates",
                        params={'instType': 'SWAP'})
        if not data or data.get('code') != '0':
            return []
        results = []
        for item in (data.get('data') or []):
            inst = item.get('instId', '')    # BTC-USDT-SWAP
            if '-USDT-SWAP' not in inst:
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': inst.replace('-USDT-SWAP', ''),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class OrangeXFetcher:
    name = 'orangex'

    def fetch(self):
        # OrangeX 使用 Deribit-style API
        data = safe_get("https://api.orangex.com/api/v1/get_funding_rate_value",
                        params={'instrument_name': 'all'})
        if not data:
            # 嘗試 tickers endpoint
            data = safe_get("https://api.orangex.com/api/v1/get_instruments",
                            params={'currency': 'USDT', 'kind': 'future'})
        if not data or data.get('result') is None:
            return []
        results = []
        for item in (data.get('result') or []):
            inst = item.get('instrument_name', '')
            if 'USDT' not in inst:
                continue
            rate = item.get('current_funding') or item.get('funding_rate')
            if rate is None:
                continue
            try:
                base = inst.split('-')[0]
                results.append({
                    'symbol': base,
                    'rate': float(rate),
                    'time': '-'
                })
            except:
                pass
        return results


class BitMartFetcher:
    name = 'bitmart'

    def fetch(self):
        # BitMart Futures: GET /contract/public/funding-rate  需 symbol
        # 先拿合約詳情列表
        detail = safe_get("https://api-cloud.bitmart.com/contract/public/details")
        if not detail or detail.get('code') != 1000:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        symbols = [
            c['symbol'] for c in (detail.get('data', {}).get('symbols') or [])
            if c.get('symbol', '').endswith('USDT')
        ]

        def fetch_one(sym):
            d = safe_get("https://api-cloud.bitmart.com/contract/public/funding-rate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 1000:
                item = d.get('data', {})
                try:
                    return {
                        'symbol': strip_usdt(sym),
                        'rate': float(item['rate_value']),
                        'time': get_tw_time(item.get('funding_time'))
                    }
                except:
                    pass
            return None

        results = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(fetch_one, s): s for s in symbols}
            for f in as_completed(futures):
                r = f.result()
                if r:
                    results.append(r)
        return results


class HotcoinFetcher:
    name = 'hotcoin'

    def fetch(self):
        data = safe_get("https://api.hotcoin.top/v1/contract/funding-rate/list",
                        params={'size': 200})
        if not data or data.get('code') != 200:
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('contractCode', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class ZoomexFetcher:
    name = 'zoomex'

    def fetch(self):
        # Zoomex 架構近似 Bybit v3
        data = safe_get("https://api.zoomex.com/v3/public/linear/tickers")
        if not data or data.get('ret_code') != 0:
            # 嘗試 v5 Bybit-style
            data = safe_get("https://openapi.zoomex.com/v5/market/tickers",
                            params={'category': 'linear'})
            if not data or data.get('retCode') != 0:
                return []
            items = data.get('result', {}).get('list', [])
            results = []
            for item in items:
                sym = item.get('symbol', '')
                if not sym.endswith('USDT') or not item.get('fundingRate'):
                    continue
                try:
                    results.append({
                        'symbol': strip_usdt(sym),
                        'rate': float(item['fundingRate']),
                        'time': get_tw_time(item.get('nextFundingTime'))
                    })
                except:
                    pass
            return results

        results = []
        for item in (data.get('result', {}).get('list') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('funding_rate') or item.get('fundingRate')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(item.get('next_funding_time') or item.get('nextFundingTime'))
                })
            except:
                pass
        return results


class BloFinFetcher:
    name = 'blofin'

    def fetch(self):
        data = safe_get("https://openapi.blofin.com/api/v1/market/funding-rate-all")
        if not data or data.get('code') != '0':
            return []
        results = []
        for item in (data.get('data') or []):
            inst = item.get('instId', '')   # BTC-USDT
            if not inst.endswith('-USDT'):
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': inst.replace('-USDT', ''),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class BitrueFetcher:
    name = 'bitrue'

    def fetch(self):
        # Bitrue 合約 API
        data = safe_get("https://fapi.bitrue.com/fapi/v1/premiumIndex")
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get('data', [])
        results = []
        for item in data:
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('lastFundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class BYDFiFetcher:
    name = 'bydfi'

    def fetch(self):
        data = safe_get("https://api.bydfi.com/api/v1/contract/funding_rates",
                        params={'limit': 200})
        if not data or data.get('code') != 0:
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('funding_rate') or item.get('fundingRate')
            next_ts = item.get('next_funding_time') or item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class CoinExFetcher:
    name = 'coinex'

    def fetch(self):
        # CoinEx v2 futures market endpoint
        data = safe_get("https://api.coinex.com/v2/futures/market",
                        params={'market_type': 'linear'})
        if not data or data.get('code') != 0:
            return []
        results = []
        for item in (data.get('data') or []):
            market = item.get('market', '')
            if not market.endswith('USDT'):
                continue
            rate = item.get('latest_funding_rate')
            next_ts = item.get('next_funding_time')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(market),
                    'rate': float(rate),
                    'time': get_tw_time(int(next_ts) * 1000 if next_ts else None)
                })
            except:
                pass
        return results


class FlipsterFetcher:
    name = 'flipster'

    def fetch(self):
        data = safe_get("https://api.flipster.io/api/v1/markets")
        if not data:
            return []
        markets = data if isinstance(data, list) else data.get('data', [])
        results = []
        for item in markets:
            sym = item.get('symbol', '') or item.get('id', '')
            if 'USDT' not in sym:
                continue
            rate = item.get('fundingRate') or item.get('funding_rate')
            next_ts = item.get('nextFundingTime') or item.get('next_funding_time')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class CoinWFetcher:
    name = 'coinw'

    def fetch(self):
        data = safe_get("https://futures.coinw.com/api/swap/v2/market/tickers")
        if not data or str(data.get('errno', '')) != '0':
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'):
                continue
            rate = item.get('fundingRate')
            next_ts = item.get('nextFundingTime')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


class BTCCFetcher:
    name = 'btcc'

    def fetch(self):
        data = safe_get("https://api.btcc.com/api/v1/public/futures/tickers")
        if not data or data.get('code') != 0:
            return []
        results = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '') or item.get('contractCode', '')
            if 'USDT' not in sym.upper():
                continue
            rate = item.get('fundingRate') or item.get('funding_rate')
            next_ts = item.get('nextFundingTime') or item.get('next_funding_time')
            if rate is None:
                continue
            try:
                results.append({
                    'symbol': strip_usdt(sym),
                    'rate': float(rate),
                    'time': get_tw_time(next_ts)
                })
            except:
                pass
        return results


# ─── 統一 Registry ────────────────────────────────────────────────────────────

ALL_FETCHERS = [
    BinanceFetcher(),
    BybitFetcher(),
    BitgetFetcher(),
    GateFetcher(),
    OKXFetcher(),
    MEXCFetcher(),
    KuCoinFetcher(),
    BingXFetcher(),
    XTFetcher(),
    LBankFetcher(),
    WEEXFetcher(),
    ToobitFetcher(),
    BitunixFetcher(),
    PhemexFetcher(),
    WhiteBITFetcher(),
    HTXFetcher(),
    PionexFetcher(),
    DeepcoinFetcher(),
    OrangeXFetcher(),
    BitMartFetcher(),
    HotcoinFetcher(),
    ZoomexFetcher(),
    BloFinFetcher(),
    BitrueFetcher(),
    BYDFiFetcher(),
    CoinExFetcher(),
    FlipsterFetcher(),
    CoinWFetcher(),
    BTCCFetcher(),
]

EXCHANGE_IDS = [f.name for f in ALL_FETCHERS]


def fetch_all(fetcher_list=None):
    """
    並行執行所有交易所的 fetch()，回傳:
    {exchange_name: [{'symbol':..,'rate':..,'time':..}, ...]}
    """
    if fetcher_list is None:
        fetcher_list = ALL_FETCHERS
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}

    def run(fetcher):
        try:
            data = fetcher.fetch()
            logger.info(f"[{fetcher.name}] 取得 {len(data)} 筆")
            return fetcher.name, data
        except Exception as e:
            logger.error(f"[{fetcher.name}] fetch 失敗: {e}")
            return fetcher.name, []

    with ThreadPoolExecutor(max_workers=len(fetcher_list)) as ex:
        futures = {ex.submit(run, f): f for f in fetcher_list}
        for future in as_completed(futures):
            name, data = future.result()
            results[name] = data

    return results
