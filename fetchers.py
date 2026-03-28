"""
worker.py  ── 跑在手機 / 本地端
抓取所有交易所資費 → POST 上傳到 Render
"""

import time
import requests
import logging
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 設定區 =====================
RENDER_UPLOAD_URL = "https://flask-j806.onrender.com/api/upload_data"  # 改成你的網址
API_SECRET        = "4132"        # 跟 Render 設定一樣
UPDATE_INTERVAL   = 60            # 秒，每輪間隔
MAX_WORKERS       = 6             # 同時跑幾個交易所（手機建議 4~8）
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("Worker")


# ─── 共用工具 ──────────────────────────────────────────────────────────────────

def get_tw_time(ts_ms=None):
    if not ts_ms:
        return '-'
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, timezone.utc) + timedelta(hours=8)
        return dt.strftime('%H:%M:%S')
    except:
        return '-'

def strip_usdt(symbol: str) -> str:
    for sep in ['/', '-', '_']:
        if sep in symbol:
            return symbol.split(sep)[0]
    return symbol[:-4] if symbol.endswith('USDT') else symbol

def safe_get(url, params=None, timeout=12, headers=None):
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"GET {url} → {e}")
        return None

def next_8h_ts_ms():
    now = time.time() * 1000
    eight = 8 * 3600 * 1000
    return (int(now) // eight + 1) * eight

def parallel(fn_list, workers=20):
    """執行一批 (callable, arg) → [result, ...]，過濾 None"""
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, arg): arg for fn, arg in fn_list}
        for f in as_completed(futures):
            r = f.result()
            if r is not None:
                results.append(r)
    return results


# ─── 各交易所 Fetcher ──────────────────────────────────────────────────────────

class BinanceFetcher:
    name = 'binance'
    def fetch(self):
        data = safe_get("https://fapi.binance.com/fapi/v1/premiumIndex")
        if not data: return []
        out = []
        for item in data:
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and item.get('lastFundingRate') is not None:
                try:
                    out.append({'symbol': strip_usdt(sym),
                                'rate': float(item['lastFundingRate']),
                                'time': get_tw_time(item.get('nextFundingTime'))})
                except: pass
        return out

class BybitFetcher:
    name = 'bybit'
    def fetch(self):
        data = safe_get("https://api.bybit.com/v5/market/tickers", params={'category': 'linear'})
        if not data or data.get('retCode') != 0: return []
        out = []
        for item in data['result']['list']:
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and item.get('fundingRate') not in (None, ''):
                try:
                    out.append({'symbol': strip_usdt(sym),
                                'rate': float(item['fundingRate']),
                                'time': get_tw_time(item.get('nextFundingTime'))})
                except: pass
        return out

class BitgetFetcher:
    name = 'bitget'
    def fetch(self):
        data = safe_get("https://api.bitget.com/api/v2/mix/market/tickers",
                        params={'productType': 'USDT-FUTURES'})
        if not data or data.get('code') != '00000': return []
        calc = next_8h_ts_ms()
        out = []
        for item in data.get('data', []):
            sym = item.get('symbol', '')
            if sym.endswith('USDT') and item.get('fundingRate') not in (None, ''):
                try:
                    out.append({'symbol': strip_usdt(sym),
                                'rate': float(item['fundingRate']),
                                'time': get_tw_time(calc)})
                except: pass
        return out

class GateFetcher:
    name = 'gate'
    def fetch(self):
        data = safe_get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
        if not data: return []
        out = []
        for item in data:
            name = item.get('name', '')
            if not name.endswith('_USDT'): continue
            rate = item.get('funding_rate')
            nxt  = item.get('funding_next_apply')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(name),
                            'rate': float(rate),
                            'time': get_tw_time(int(nxt)*1000 if nxt else None)})
            except: pass
        return out

class OKXFetcher:
    name = 'okx'
    def fetch(self):
        ticker_data = safe_get("https://www.okx.com/api/v5/market/tickers",
                               params={'instType': 'SWAP'})
        if not ticker_data or ticker_data.get('code') != '0': return []
        inst_ids = [t['instId'] for t in ticker_data.get('data', [])
                    if t['instId'].endswith('-USDT-SWAP')]

        def one(inst_id):
            d = safe_get("https://www.okx.com/api/v5/public/funding-rate",
                         params={'instId': inst_id}, timeout=8)
            if d and d.get('code') == '0' and d.get('data'):
                item = d['data'][0]
                try:
                    return {'symbol': inst_id.replace('-USDT-SWAP', ''),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, iid) for iid in inst_ids], workers=25)

class MEXCFetcher:
    name = 'mexc'
    def fetch(self):
        data = safe_get("https://contract.mexc.com/api/v1/contract/funding_rate")
        if not data or data.get('success') is not True: return []
        out = []
        for item in data.get('data', []):
            sym = item.get('symbol', '')
            if not sym.endswith('_USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextSettleTime'))})
            except: pass
        return out

class KuCoinFetcher:
    name = 'kucoin'
    def fetch(self):
        cd = safe_get("https://api-futures.kucoin.com/api/v1/contracts/active")
        if not cd or cd.get('code') != '200000': return []
        syms = [c['symbol'] for c in cd.get('data', [])
                if c.get('symbol', '').endswith('USDTM')]

        def one(sym):
            d = safe_get(f"https://api-futures.kucoin.com/api/v1/funding-rate/{sym}/current",
                         timeout=8)
            if d and d.get('code') == '200000' and d.get('data'):
                item = d['data']
                try:
                    return {'symbol': sym.replace('USDTM', ''),
                            'rate': float(item['value']),
                            'time': get_tw_time(item.get('timePoint'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

class BingXFetcher:
    name = 'bingx'
    def fetch(self):
        cd = safe_get("https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
        if not cd or cd.get('code') != 0: return []
        syms = [c['symbol'] for c in cd.get('data', [])
                if str(c.get('symbol', '')).endswith('-USDT')]

        def one(sym):
            d = safe_get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 0 and d.get('data'):
                item = d['data']
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

class XTFetcher:
    name = 'xt'
    def fetch(self):
        data = safe_get("https://fapi.xt.com/future/market/v1/public/q/funding-rate-list",
                        params={'pageSize': 500, 'page': 1})
        if not data or str(data.get('returnCode')) != '0': return []
        out = []
        for item in data.get('result', {}).get('items', []):
            sym = item.get('contractName', '')
            if not sym.endswith('_USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class LBankFetcher:
    name = 'lbank'
    def fetch(self):
        data = safe_get("https://www.lbkex.net/v2/contract/funding-rates.do",
                        params={'size': 200, 'current': 1})
        if not data or data.get('result') != 'true':
            data = safe_get("https://api.lbkex.com/contract/funding-rates.do",
                            params={'size': 200, 'current': 1})
        if not data or data.get('result') != 'true': return []
        out = []
        for item in data.get('data', {}).get('records', []):
            sym = item.get('contractId', '')
            if not sym.endswith('_USDT'): continue
            rate = item.get('currentRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingAt'))})
            except: pass
        return out

class WEEXFetcher:
    name = 'weex'
    def fetch(self):
        info = safe_get("https://api-contract.weex.com/capi/v3/market/contracts")
        if not info or info.get('code') != 200:
            return self._v2()
        syms = [c['symbol'] for c in (info.get('data') or [])
                if str(c.get('symbol', '')).endswith('USDT')]

        def one(sym):
            d = safe_get("https://api-contract.weex.com/capi/v3/market/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 200 and d.get('data'):
                item = d['data']
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

    def _v2(self):
        data = safe_get("https://api-contract.weex.com/capi/v2/market/tickers")
        if not data or data.get('code') != 200: return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class ToobitFetcher:
    name = 'toobit'
    def fetch(self):
        info = safe_get("https://api.toobit.com/api/v1/exchangeInfo")
        if not info: return []
        syms = [s['contractName'] for s in info.get('contracts', [])
                if str(s.get('contractName', '')).endswith('USDT')]

        def one(sym):
            d = safe_get("https://api.toobit.com/swap/v1/market/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('fundingRate') is not None:
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(d['fundingRate']),
                            'time': get_tw_time(d.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

class BitunixFetcher:
    name = 'bitunix'
    def fetch(self):
        data = safe_get("https://fapi.bitunix.com/api/v1/futures/market/batch_funding_rate")
        if data and data.get('code') == 0:
            out = []
            for item in (data.get('data') or []):
                sym = item.get('symbol', '')
                if not sym.endswith('USDT'): continue
                rate = item.get('fundingRate')
                if rate is None: continue
                try:
                    out.append({'symbol': strip_usdt(sym),
                                'rate': float(rate),
                                'time': get_tw_time(item.get('nextFundingTime'))})
                except: pass
            return out
        # fallback 逐一
        pd = safe_get("https://fapi.bitunix.com/api/v1/futures/market/get_trading_pairs")
        if not pd or pd.get('code') != 0: return []
        syms = [p['symbol'] for p in (pd.get('data') or []) if p.get('symbol','').endswith('USDT')]

        def one(sym):
            d = safe_get("https://fapi.bitunix.com/api/v1/futures/market/funding_rate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 0 and d.get('data'):
                item = d['data']
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

class PhemexFetcher:
    name = 'phemex'
    def fetch(self):
        data = safe_get("https://api.phemex.com/md/v3/ticker/24hr/all",
                        params={'type': 'Perpetual'})
        if not data or data.get('code') != 0: return []
        out = []
        for item in (data.get('data', {}).get('tickers') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'): continue
            rate = item.get('fundingRateEr') or item.get('fundingRate')
            if rate is None: continue
            try:
                rate_f = float(rate)/1e8 if abs(float(rate)) > 1 else float(rate)
                nxt = item.get('nextFundingTimeEp')
                out.append({'symbol': strip_usdt(sym),
                            'rate': rate_f,
                            'time': get_tw_time(nxt*1000 if nxt else None)})
            except: pass
        return out

class WhiteBITFetcher:
    name = 'whitebit'
    def fetch(self):
        data = safe_get("https://whitebit.com/api/v4/public/futures")
        if not data: return []
        out = []
        for sym, info in data.items():
            if not (sym.endswith('_USDT') or sym.endswith('/USDT')): continue
            rate = info.get('fundingRate') or info.get('funding_rate')
            nxt  = info.get('nextFundingTime') or info.get('next_funding_time')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(nxt)})
            except: pass
        return out

class HTXFetcher:
    name = 'htx'
    def fetch(self):
        data = safe_get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate")
        if not data or data.get('status') != 'ok': return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('contract_code', '')
            if not sym.endswith('-USDT'): continue
            rate = item.get('funding_rate')
            if rate is None: continue
            try:
                out.append({'symbol': sym.replace('-USDT',''),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('funding_time'))})
            except: pass
        return out

class PionexFetcher:
    name = 'pionex'
    def fetch(self):
        info = safe_get("https://api.pionex.com/api/v1/market/tickers", params={'type': 'PERP'})
        if not info or info.get('result') is not True: return []
        syms = [t['symbol'] for t in (info.get('data', {}).get('tickers') or [])
                if str(t.get('symbol', '')).endswith('_USDT')]

        def one(sym):
            d = safe_get("https://api.pionex.com/api/v1/market/perpetual/fundingRate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('result') is True and d.get('data'):
                item = d['data']
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=15)

class DeepcoinFetcher:
    name = 'deepcoin'
    def fetch(self):
        data = safe_get("https://api.deepcoin.com/deepcoin/swap/fundingRates",
                        params={'instType': 'SWAP'})
        if not data or data.get('code') != '0': return []
        out = []
        for item in (data.get('data') or []):
            inst = item.get('instId', '')
            if '-USDT-SWAP' not in inst: continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': inst.replace('-USDT-SWAP',''),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class OrangeXFetcher:
    name = 'orangex'
    def fetch(self):
        data = safe_get("https://api.orangex.com/api/v1/get_funding_rate_value",
                        params={'instrument_name': 'all'})
        if not data or data.get('result') is None:
            data = safe_get("https://api.orangex.com/api/v1/get_instruments",
                            params={'currency': 'USDT', 'kind': 'future'})
        if not data or data.get('result') is None: return []
        out = []
        for item in (data.get('result') or []):
            inst = item.get('instrument_name', '')
            if 'USDT' not in inst: continue
            rate = item.get('current_funding') or item.get('funding_rate')
            if rate is None: continue
            try:
                out.append({'symbol': inst.split('-')[0],
                            'rate': float(rate),
                            'time': '-'})
            except: pass
        return out

class BitMartFetcher:
    name = 'bitmart'
    def fetch(self):
        detail = safe_get("https://api-cloud.bitmart.com/contract/public/details")
        if not detail or detail.get('code') != 1000: return []
        syms = [c['symbol'] for c in (detail.get('data', {}).get('symbols') or [])
                if c.get('symbol', '').endswith('USDT')]

        def one(sym):
            d = safe_get("https://api-cloud.bitmart.com/contract/public/funding-rate",
                         params={'symbol': sym}, timeout=8)
            if d and d.get('code') == 1000:
                item = d.get('data', {})
                try:
                    return {'symbol': strip_usdt(sym),
                            'rate': float(item['rate_value']),
                            'time': get_tw_time(item.get('funding_time'))}
                except: pass
            return None

        return parallel([(one, s) for s in syms], workers=20)

class HotcoinFetcher:
    name = 'hotcoin'
    def fetch(self):
        data = safe_get("https://api.hotcoin.top/v1/contract/funding-rate/list",
                        params={'size': 200})
        if not data or data.get('code') != 200: return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('contractCode', '')
            if not sym.endswith('USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class ZoomexFetcher:
    name = 'zoomex'
    def fetch(self):
        data = safe_get("https://openapi.zoomex.com/v5/market/tickers",
                        params={'category': 'linear'})
        if not data or data.get('retCode') != 0: return []
        out = []
        for item in data.get('result', {}).get('list', []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT') or not item.get('fundingRate'): continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(item['fundingRate']),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class BloFinFetcher:
    name = 'blofin'
    def fetch(self):
        data = safe_get("https://openapi.blofin.com/api/v1/market/funding-rate-all")
        if not data or data.get('code') != '0': return []
        out = []
        for item in (data.get('data') or []):
            inst = item.get('instId', '')
            if not inst.endswith('-USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': inst.replace('-USDT',''),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class BitrueFetcher:
    name = 'bitrue'
    def fetch(self):
        data = safe_get("https://fapi.bitrue.com/fapi/v1/premiumIndex")
        if not data: return []
        if isinstance(data, dict): data = data.get('data', [])
        out = []
        for item in data:
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'): continue
            rate = item.get('lastFundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class BYDFiFetcher:
    name = 'bydfi'
    def fetch(self):
        data = safe_get("https://api.bydfi.com/api/v1/contract/funding_rates",
                        params={'limit': 200})
        if not data or data.get('code') != 0: return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('symbol', '')
            if not sym.endswith('USDT'): continue
            rate = item.get('funding_rate') or item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('next_funding_time') or item.get('nextFundingTime'))})
            except: pass
        return out

class CoinExFetcher:
    name = 'coinex'
    def fetch(self):
        data = safe_get("https://api.coinex.com/v2/futures/market",
                        params={'market_type': 'linear'})
        if not data or data.get('code') != 0: return []
        out = []
        for item in (data.get('data') or []):
            market = item.get('market', '')
            if not market.endswith('USDT'): continue
            rate = item.get('latest_funding_rate')
            nxt  = item.get('next_funding_time')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(market),
                            'rate': float(rate),
                            'time': get_tw_time(int(nxt)*1000 if nxt else None)})
            except: pass
        return out

class FlipsterFetcher:
    name = 'flipster'
    def fetch(self):
        data = safe_get("https://api.flipster.io/api/v1/markets")
        if not data: return []
        markets = data if isinstance(data, list) else data.get('data', [])
        out = []
        for item in markets:
            sym = item.get('symbol','') or item.get('id','')
            if 'USDT' not in sym: continue
            rate = item.get('fundingRate') or item.get('funding_rate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime') or item.get('next_funding_time'))})
            except: pass
        return out

class CoinWFetcher:
    name = 'coinw'
    def fetch(self):
        data = safe_get("https://futures.coinw.com/api/swap/v2/market/tickers")
        if not data or str(data.get('errno','')) != '0': return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('symbol','')
            if not sym.endswith('USDT'): continue
            rate = item.get('fundingRate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime'))})
            except: pass
        return out

class BTCCFetcher:
    name = 'btcc'
    def fetch(self):
        data = safe_get("https://api.btcc.com/api/v1/public/futures/tickers")
        if not data or data.get('code') != 0: return []
        out = []
        for item in (data.get('data') or []):
            sym = item.get('symbol','') or item.get('contractCode','')
            if 'USDT' not in sym.upper(): continue
            rate = item.get('fundingRate') or item.get('funding_rate')
            if rate is None: continue
            try:
                out.append({'symbol': strip_usdt(sym),
                            'rate': float(rate),
                            'time': get_tw_time(item.get('nextFundingTime') or item.get('next_funding_time'))})
            except: pass
        return out


# ─── Registry ──────────────────────────────────────────────────────────────────

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


# ─── 聚合 ──────────────────────────────────────────────────────────────────────

def aggregate(raw: dict) -> list:
    agg = {}
    for exchange, items in raw.items():
        for item in items:
            sym = item['symbol']
            if sym not in agg:
                agg[sym] = {'symbol': sym}
                for ex in EXCHANGE_IDS:
                    agg[sym][f'{ex}_rate'] = None
                    agg[sym][f'{ex}_time'] = '-'
            agg[sym][f'{exchange}_rate'] = item['rate']
            agg[sym][f'{exchange}_time'] = item.get('time', '-')
    lst = list(agg.values())
    lst.sort(key=lambda x: x.get('binance_rate') if x.get('binance_rate') is not None else 999)
    return lst


# ─── 主迴圈 ────────────────────────────────────────────────────────────────────

def main():
    logger.info(f"Worker 啟動，共 {len(ALL_FETCHERS)} 個交易所，上傳目標: {RENDER_UPLOAD_URL}")

    while True:
        try:
            logger.info("=== 開始抓取 ===")
            raw = {}

            def run_fetcher(fetcher):
                try:
                    data = fetcher.fetch()
                    logger.info(f"  [{fetcher.name}] {len(data)} 筆")
                    return fetcher.name, data
                except Exception as e:
                    logger.error(f"  [{fetcher.name}] 失敗: {e}")
                    return fetcher.name, []

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = {ex.submit(run_fetcher, f): f for f in ALL_FETCHERS}
                for future in as_completed(futures):
                    name, data = future.result()
                    raw[name] = data

            final_list = aggregate(raw)
            stats = {ex: len(raw.get(ex, [])) for ex in EXCHANGE_IDS}
            total = len(final_list)
            logger.info(f"=== 聚合完成: {total} 幣種 ===")

            # ── 上傳到 Render ──
            payload = {
                "updated_at": datetime.now().strftime('%H:%M:%S'),
                "data": final_list,
                "exchange_stats": stats
            }
            try:
                res = requests.post(
                    RENDER_UPLOAD_URL,
                    json=payload,
                    headers={'X-Api-Key': API_SECRET},
                    timeout=30
                )
                if res.status_code == 200:
                    logger.info(f"✅ 上傳成功 ({total} 筆)")
                else:
                    logger.error(f"❌ 上傳失敗: {res.status_code} {res.text[:200]}")
            except Exception as e:
                logger.error(f"❌ 上傳錯誤: {e}")

        except Exception as e:
            logger.error(f"主迴圈崩潰: {e}")

        logger.info(f"休息 {UPDATE_INTERVAL} 秒...\n")
        time.sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
