"""
app.py  ── Flask 主程式
"""

from flask import Flask, render_template, jsonify
import time
import os
import threading
import logging
from datetime import datetime
from fetchers import ALL_FETCHERS, EXCHANGE_IDS, fetch_all

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

UPDATE_INTERVAL = 120   # 秒

# ─── 全局資料容器 ──────────────────────────────────────────────────────────────

global_data_store = {
    "timestamp": 0,
    "rates": [],
    "status": "initializing",
    "last_success": None,
    "error_msg": None,
    "exchange_stats": {}   # {exchange: count}
}

_bg_thread = None
_thread_lock = threading.Lock()

# ─── 後台更新任務 ──────────────────────────────────────────────────────────────

def aggregate(raw: dict) -> list:
    """
    raw: {exchange: [{'symbol','rate','time'}, ...]}
    → [{'symbol': 'BTC', '<ex>_rate': float|None, '<ex>_time': str}, ...]
    """
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

    final = list(agg.values())
    final.sort(key=lambda x: x.get('binance_rate') if x.get('binance_rate') is not None else 999)
    return final


def update_loop():
    logger.info("後台更新執行緒已啟動")
    while True:
        try:
            logger.info("開始抓取所有交易所...")
            raw = fetch_all()

            stats = {ex: len(data) for ex, data in raw.items()}
            final_list = aggregate(raw)

            if final_list:
                global_data_store['rates'] = final_list
                global_data_store['timestamp'] = time.time()
                global_data_store['last_success'] = datetime.now().strftime('%H:%M:%S')
                global_data_store['status'] = 'updated'
                global_data_store['exchange_stats'] = stats
                logger.info(f"更新成功: {len(final_list)} 幣種，各所筆數: {stats}")
            else:
                global_data_store['status'] = 'empty'
                global_data_store['error_msg'] = '全部交易所皆無資料'

        except Exception as e:
            logger.error(f"後台更新崩潰: {e}")
            global_data_store['status'] = 'error'
            global_data_store['error_msg'] = str(e)

        time.sleep(UPDATE_INTERVAL)


def start_background_thread():
    global _bg_thread
    with _thread_lock:
        if _bg_thread is None or not _bg_thread.is_alive():
            _bg_thread = threading.Thread(target=update_loop, daemon=True)
            _bg_thread.start()

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    start_background_thread()
    return render_template('index.html', exchanges=EXCHANGE_IDS)


@app.route('/api/rates')
def api_rates():
    start_background_thread()
    return jsonify({
        'count': len(global_data_store['rates']),
        'data': global_data_store['rates'],
        'updated_at': global_data_store['last_success'] or '初始載入中...',
        'status': global_data_store['status'],
        'exchange_stats': global_data_store.get('exchange_stats', {})
    })


@app.route('/api/upload_data', methods=['POST'])
def upload_data():
    """給 Android Worker 推送用（保留相容）"""
    from flask import request
    secret = request.headers.get('X-Api-Key', '')
    if secret != os.environ.get('API_SECRET', '4132'):
        return jsonify({'error': 'unauthorized'}), 401
    payload = request.get_json(force=True)
    if payload and payload.get('data'):
        global_data_store['rates'] = payload['data']
        global_data_store['last_success'] = payload.get('updated_at', '?')
        global_data_store['status'] = 'updated'
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
