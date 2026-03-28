"""
app.py  ── 跑在 Render
只負責：
  1. 接收 worker.py 推送的資料 (POST /api/upload_data)
  2. 提供前端頁面與 API (GET /  GET /api/rates)
"""

from flask import Flask, render_template, jsonify, request
import os
import threading
import logging
from fetchers import EXCHANGE_IDS   # 只為了讓 index.html 知道欄位順序

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

API_SECRET = os.environ.get('API_SECRET', '4132')

# ─── 全局資料容器 (由 worker POST 進來更新) ───────────────────────────────────
data_store = {
    "rates": [],
    "updated_at": "尚未收到資料",
    "status": "waiting",
    "exchange_stats": {}
}
store_lock = threading.Lock()


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', exchanges=EXCHANGE_IDS)


@app.route('/api/rates')
def api_rates():
    with store_lock:
        return jsonify({
            'count':          len(data_store['rates']),
            'data':           data_store['rates'],
            'updated_at':     data_store['updated_at'],
            'status':         data_store['status'],
            'exchange_stats': data_store['exchange_stats']
        })


@app.route('/api/upload_data', methods=['POST'])
def upload_data():
    if request.headers.get('X-Api-Key', '') != API_SECRET:
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(force=True, silent=True)
    if not payload:
        return jsonify({'error': 'bad json'}), 400

    with store_lock:
        data_store['rates']          = payload.get('data', [])
        data_store['updated_at']     = payload.get('updated_at', '?')
        data_store['exchange_stats'] = payload.get('exchange_stats', {})
        data_store['status']         = 'updated'

    count = len(data_store['rates'])
    logger.info(f"收到推送: {count} 筆, updated_at={data_store['updated_at']}")
    return jsonify({'ok': True, 'count': count})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
