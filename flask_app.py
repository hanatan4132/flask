from flask import Flask, render_template, jsonify, request
import os
import json
from datetime import datetime

app = Flask(__name__)

# === 設定區 ===
# 設定一個密碼，防止別人亂傳假資料給你
# 你可以在 Render 的 Environment Variables 設定，或是直接寫在這裡
API_SECRET = os.environ.get('API_SECRET')

# 全局數據容器 (預設為空)
global_data_store = {
    "updated_at": "等待手機上傳...",
    "rates": []
}

@app.route('/')
def index():
    # 這裡沿用你原本漂亮的手機版 HTML (請確認 templates/index.html 還在)
    # 我們只需要把 exchanges 列表傳進去，讓表頭能渲染
    return render_template('index.html', exchanges=['binance', 'bybit', 'bitget'])

# === 新增：接收資料的接口 (給手機用) ===
@app.route('/api/upload_data', methods=['POST'])
def upload_data():
    # 1. 檢查密碼 (安全性)
    auth_header = request.headers.get('X-Api-Key')
    if auth_header != API_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        # 2. 接收 JSON 資料
        payload = request.json
        
        # 3. 更新記憶體中的數據
        # 手機傳來的格式預計是: { "updated_at": "...", "data": [...] }
        global_data_store['rates'] = payload.get('data', [])
        global_data_store['updated_at'] = payload.get('updated_at', datetime.now().strftime('%H:%M:%S'))
        
        print(f"收到更新！共 {len(global_data_store['rates'])} 筆資料")
        return jsonify({"status": "success", "count": len(global_data_store['rates'])})
        
    except Exception as e:
        print(f"資料處理錯誤: {e}")
        return jsonify({"error": str(e)}), 500

# === 前端網頁獲取資料的接口 ===
@app.route('/api/rates')
def api_rates():
    # 直接回傳目前記憶體裡的資料
    return jsonify({
        "data": global_data_store['rates'],
        "updated_at": global_data_store['updated_at']
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
