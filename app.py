from flask import Flask, render_template, jsonify, request, send_from_directory
import json
import os
import requests
from datetime import datetime, timedelta
import sqlite3 

app = Flask(__name__)

# --- API 키 설정 ---
# (v10.0) app.py는 "무료 키"를 사용 (5 API/분)
API_KEY = "YmpGnmOSQHvi8fVMOB1gIyp_aqcMeqmp"

# --- (v3.11) DB 경로 ---
DB_FILE = "danso.db"

# --- 1. 메인 페이지 라우트 ---
@app.route('/')
def dashboard_page():
    return render_template('index.html')

# --- ✅ (PWA 수정) 2. PWA 파일 서빙 라우트 (sw.js, manifest.json) ---

@app.route('/sw.js')
def serve_sw():
    """서비스 워커 파일을 루트에서 서빙합니다."""
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def serve_manifest():
    """매니페스트 파일을 루트에서 서빙합니다."""
    return send_from_directory('.', 'manifest.json', mimetype='application/manifest+json')

# (보너스) 404 오류를 없애기 위한 파비콘 라우트
@app.route('/favicon.ico')
def serve_favicon():
    """파비콘 파일을 서빙합니다. (danso_logo.png 재활용)"""
    return send_from_directory(os.path.join(app.root_path, 'static', 'images'), 
                               'danso_logo.png', mimetype='image/png')

# --- 3. (v10.0) 대시보드 데이터 API (DB 사용) ---
@app.route('/api/dashboard')
def get_dashboard_data():
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT value FROM status WHERE key = 'status_data' ORDER BY last_updated DESC LIMIT 1")
        status_row = cursor.fetchone()
        if status_row:
            status = json.loads(status_row['value'])
        else:
            status = {'last_scan_time': 'N/A', 'watching_count': 0, 'watching_tickers': []}
            
        cursor.execute("SELECT ticker, price, time FROM signals ORDER BY time DESC LIMIT 50")
        signals = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute("SELECT ticker, price, time, probability_score FROM recommendations ORDER BY time DESC LIMIT 50")
        recommendations = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return jsonify({
            'status': status,
            'signals': signals,
            'recommendations': recommendations
        })
    except Exception as e:
        if conn: conn.close()
        return jsonify({
            'status': {'last_scan_time': 'Scanner waiting...', 'watching_count': 0, 'watching_tickers': []},
            'signals': [], 'recommendations': []
        })

# --- 4. (v3.11) 커뮤니티 API (게시글 읽기) ---
@app.route('/api/posts')
def get_posts():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT author, content, time FROM posts ORDER BY time DESC LIMIT 100")
        posts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"status": "OK", "posts": posts})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 5. (v3.11) 커뮤니티 API (게시글 쓰기) ---
@app.route('/api/posts', methods=['POST'])
def create_post():
    try:
        data = request.get_json()
        author = data.get('author', 'Anonymous')
        content = data.get('content')
        
        if not content:
            return jsonify({"status": "error", "message": "Content is empty."}), 400
            
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO posts (author, content, time) VALUES (?, ?, ?)",
                       (author, content, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "OK", "message": "Post created."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# --- 6. 실시간 호가 API ---
@app.route('/api/quote/<string:ticker>')
def get_quote(ticker):
    url = f"https://api.polygon.io/v3/quotes/{ticker.upper()}?limit=1&apiKey={API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            return jsonify(data['results'][0])
        else:
            return jsonify({"status": "error", "message": "Ticker not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 7. 기업 상세 정보 + 재무제표 API ---
@app.route('/api/details/<string:ticker>')
def get_ticker_details(ticker):
    # ✅ (500 에러 수정) 누락되었던 API URL을 복구합니다.
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}?apiKey={API_KEY}"
    try:
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            results = data['results']
            logo_url = results.get('branding', {}).get('logo_url', '')
            if logo_url: logo_url += f"?apiKey={API_KEY}"
            
            # (v12.0) v3 API 응답이 변경되어 재무/비율 데이터 가져오는 방식 수정
            financials_node = results.get('financials', {})
            market_cap = financials_node.get('market_capitalization', {}).get('value', 'N/A')
            dividend_yield = financials_node.get('dividend_yield', {}).get('value', 'N/A')
            
            # v3에서는 ratios가 별도 노드가 아닐 수 있습니다. (데이터 확인 필요)
            # 우선 P/E는 financial > fundamental에서 가져오도록 시도
            pe_ratio = financials_node.get('price_to_earnings_ratio', 'N/A') # (경로 추정)
            ps_ratio = financials_node.get('price_to_sales_ratio', 'N/A') # (경로 추정)

            financial_data = {
                "market_cap": market_cap,
                "pe_ratio": pe_ratio,
                "ps_ratio": ps_ratio,
                "dividend_yield": dividend_yield
            }
            
            details = {
                "ticker": results.get('ticker'), "name": results.get('name'),
                "industry": results.get('sic_description'),
                "description": results.get('description', 'No description available.'),
                "logo_url": logo_url, "financials": financial_data
            }
            return jsonify({"status": "OK", "results": details})
        else:
            return jsonify({"status": "error", "message": "Ticker details not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 8. 1분봉 차트 데이터 API ---
@app.route('/api/chart_data/<string:ticker>')
def get_chart_data(ticker):
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        # (v12.0) 차트 데이터 조회를 3일 전이 아닌 7일 전으로 늘림 (주말 대비)
        past_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        # ✅ (500 에러 수정) 누락되었던 API URL을 복구합니다.
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{past_date}/{today}?sort=asc&limit=5000&apiKey={API_KEY}"
        
        response = requests.get(url)
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            chart_data = []
            for bar in data['results']:
                chart_data.append({
                    "time": bar['t'] / 1000, "open": bar['o'],
                    "high": bar['h'], "low": bar['l'], "close": bar.get('c', bar['o'])
                })
            return jsonify({"status": "OK", "results": chart_data})
        else:
            return jsonify({"status": "error", "message": "Chart data not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 9. (v3.7) 시장 개요 API ---
@app.route('/api/market_overview')
def get_market_overview():
    try:
        gainers_data = []
        losers_data = []

        # ✅ (500 에러 수정) 누락되었던 API URL을 복구합니다.
        url_g = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={API_KEY}"
        res_g = requests.get(url_g); res_g.raise_for_status() 
        gainers_data = res_g.json().get('tickers') or []
        
        # ✅ (500 에러 수정) 누락되었던 API URL을 복구합니다.
        url_l = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={API_KEY}"
        res_l = requests.get(url_l); res_l.raise_for_status() 
        losers_data = res_l.json().get('tickers') or []

        return jsonify({"status": "OK", "indices": {}, "gainers": gainers_data, "losers": losers_data})
    
    except requests.exceptions.HTTPError as http_err:
        return jsonify({"status": "error", "message": f"HTTP error: {http_err}"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    # 5000번 포트 (v10.0)
    app.run(debug=True, port=5000) # (v12.0) 디버그 모드를 True로 변경