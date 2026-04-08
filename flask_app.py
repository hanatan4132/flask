import json
from flask import Flask, render_template, jsonify, request
import os
import threading
import logging
from fetchers import EXCHANGE_IDS   # 只為了讓 index.html 知道欄位順序
import zlib
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

    try:
        # 判斷是否為壓縮資料
        if request.headers.get('Content-Encoding') == 'gzip':
            raw_data = zlib.decompress(request.get_data())
            payload = json.loads(raw_data)
        else:
            payload = request.get_json(force=True, silent=True)

        if not payload:
            return jsonify({'error': 'bad content'}), 400

        with store_lock:
            data_store['rates']          = payload.get('data', [])
            data_store['updated_at']     = payload.get('updated_at', '?')
            data_store['exchange_stats'] = payload.get('exchange_stats', {})
            data_store['status']         = 'updated'

        return jsonify({'ok': True, 'count': len(data_store['rates'])})
    except Exception as e:
        logger.error(f"處理上傳資料失敗: {e}")
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
