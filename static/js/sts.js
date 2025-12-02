import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js';

/* ==========================================================================
   PART 0. FIREBASE CONFIG
   ========================================================================== */
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

const app = initializeApp(firebaseConfig);
const messaging = getMessaging(app);
window.currentFCMToken = null;

/* ==========================================================================
   PART 1. GLOBAL STATE & DOM ELEMENTS
   ========================================================================== */
let chart = null;
let candleSeries = null;
let currentTicker = null;
let marketDataMap = {}; // 실시간 데이터를 저장해두는 맵

// Webull 스타일 HTML ID 매핑 (HTML 수정본과 일치)
const els = {
    scannerList: document.getElementById('ticker-list-container'),
    chartContainer: document.getElementById('chart-container'),
    signals: document.getElementById('signal-feed-container'),
    
    // Status
    statusText: document.getElementById('scan-status-text'),
    countText: document.getElementById('scan-watching-count'),
    
    // Chart Overlay
    overlayTicker: document.getElementById('overlay-ticker'),
    overlayPrice: document.getElementById('overlay-price'),
    
    // 🔥 [NEW] Key Statistics Metrics (Webull Panel)
    indObi: document.getElementById('ind-obi'),
    indObiMom: document.getElementById('ind-obi-mom'),
    indVpin: document.getElementById('ind-vpin'),
    indTickSpeed: document.getElementById('ind-tick-speed'),
    indTickAccel: document.getElementById('ind-tick-accel'),
    
    indVwapDist: document.getElementById('ind-vwap-dist'),
    indVwapSlope: document.getElementById('ind-vwap-slope'),
    indSqueeze: document.getElementById('ind-squeeze'),
    indRvol: document.getElementById('ind-rvol'),
    indAtr: document.getElementById('ind-atr'),
    
    indPumpAccel: document.getElementById('ind-pump-accel'),
    indSpread: document.getElementById('ind-spread'),
    indTimestamp: document.getElementById('ind-timestamp'),
    indScore: document.getElementById('ind-score'),
    indProb: document.getElementById('ind-prob')
};

/* ==========================================================================
   PART 2. DATA POLLING & RENDERING
   ========================================================================== */

async function updateDashboard() {
    try {
        const res = await fetch('/api/sts/status');
        if (!res.ok) return;
        
        const data = await res.json();
        if (!data || !data.targets) return;

        // 1. 데이터 맵핑 저장 (클릭 시 즉시 로딩용)
        data.targets.forEach(t => {
            marketDataMap[t.ticker] = t;
        });

        // 2. 좌측 스캐너 리스트 렌더링
        renderScannerList(data.targets);
        
        // 3. 현재 보고 있는 종목 실시간 갱신
        if (currentTicker && marketDataMap[currentTicker]) {
            updateKeyStats(marketDataMap[currentTicker]);
        }

        // 4. 상태 텍스트
        if(els.statusText) els.statusText.innerText = "Active (V9.3)";
        if(els.countText) els.countText.innerText = `${data.targets.length} Targets`;

        // 5. 시그널 로그
        if (data.logs) renderSignals(data.logs);

    } catch (e) {
        console.error("Sync Error:", e);
    }
}

function renderScannerList(targets) {
    if (!els.scannerList) return;
    els.scannerList.innerHTML = '';

    if (targets.length === 0) {
        els.scannerList.innerHTML = `<div style="padding:20px; text-align:center; color:#999;">Scanning...</div>`;
        return;
    }

    targets.forEach(item => {
        // AI Score 또는 Prob 사용
        const score = Math.round(item.ai_score || item.ai_prob || 0);
        const price = parseFloat(item.price).toFixed(2);
        const isActive = (item.ticker === currentTicker) ? 'background:#EBF5FF; border-left:3px solid #007AFF;' : '';

        const html = `
            <div class="ticker-row" style="${isActive}; cursor:pointer; padding:10px; border-bottom:1px solid #eee;" onclick="selectTicker('${item.ticker}')">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <div style="font-weight:800; font-size:14px;">${item.ticker}</div>
                        <div style="font-size:10px; color:#666;">Score <span style="font-weight:bold; color:${score>=80?'#007AFF':'#333'}">${score}</span></div>
                    </div>
                    <div style="font-family:'JetBrains Mono'; font-weight:600;">$${price}</div>
                </div>
            </div>`;
        els.scannerList.insertAdjacentHTML('beforeend', html);
    });
}

// 🔥 [핵심] 하단 Webull 패널 데이터 채우기
function updateKeyStats(data) {
    // 1. Chart Overlay
    if(els.overlayTicker) els.overlayTicker.innerText = data.ticker;
    if(els.overlayPrice) els.overlayPrice.innerText = `$${parseFloat(data.price).toFixed(2)}`;

    // 2. Helper for formatting
    const fmt = (val, fixed=2) => val ? parseFloat(val).toFixed(fixed) : '--';
    const color = (val) => parseFloat(val) > 0 ? '#34C759' : (parseFloat(val) < 0 ? '#FF3B30' : '#333');

    // 3. Fill Data Grid
    if(els.indObi) {
        els.indObi.innerText = fmt(data.obi);
        els.indObi.style.color = color(data.obi);
    }
    if(els.indObiMom) els.indObiMom.innerText = fmt(data.obi_mom); // DB에 컬럼 있는지 확인 필요 (없으면 0)
    
    if(els.indVpin) {
        els.indVpin.innerText = fmt(data.vpin);
        els.indVpin.style.color = data.vpin > 0.8 ? '#FF3B30' : '#333';
    }
    
    if(els.indTickSpeed) els.indTickSpeed.innerText = data.tick_speed || '0';
    if(els.indTickAccel) {
        // 틱 가속도는 DB 컬럼 추가 안 했으면 계산된 값 없을 수 있음 -> 일단 패스하거나 0
        els.indTickAccel.innerText = '0'; 
    }

    if(els.indVwapDist) {
        els.indVwapDist.innerText = fmt(data.vwap_dist) + '%';
        els.indVwapDist.style.color = color(data.vwap_dist);
    }
    
    // DB에 저장되지 않는 실시간 계산 값들은 일단 화면엔 표시하되 데이터가 없으면 '--' 처리
    if(els.indVwapSlope) els.indVwapSlope.innerText = '--'; 
    if(els.indSqueeze) els.indSqueeze.innerText = '--';
    if(els.indRvol) els.indRvol.innerText = '--';
    
    // ATR, Spread 등은 DB에 없으면 표시 불가. (엔진 업그레이드 시 DB 컬럼도 늘려야 함)
    // 하지만 현재는 주요 지표(Score, Price, OBI, VPIN) 위주로 표시
    
    if(els.indScore) {
        const score = Math.round(data.ai_score || 0);
        els.indScore.innerText = score;
        els.indScore.style.color = score >= 80 ? '#007AFF' : '#333';
        
        if(els.indProb) els.indProb.innerText = `${Math.min(99, Math.round(score * 0.95))}%`;
    }
    
    if(els.indTimestamp) els.indTimestamp.innerText = new Date().toLocaleTimeString();
}

function renderSignals(logs) {
    if (!els.signals) return;
    els.signals.innerHTML = '';
    logs.forEach(log => {
        const html = `
            <div style="padding:10px; border-bottom:1px solid #eee;">
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span style="background:#34c759; color:white; padding:2px 6px; border-radius:4px; font-size:9px; font-weight:bold;">BUY</span>
                    <span style="font-size:10px; color:#999;">${log.timestamp.split(' ')[1] || log.timestamp}</span>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:bold;">${log.ticker}</span>
                    <span style="font-family:'JetBrains Mono';">$${log.price}</span>
                </div>
            </div>`;
        els.signals.insertAdjacentHTML('beforeend', html);
    });
}

/* ==========================================================================
   PART 3. CHART ENGINE
   ========================================================================== */

// 전역 함수 등록
window.selectTicker = async function(ticker) {
    currentTicker = ticker;
    // 1. 데이터 있으면 즉시 패널 갱신
    if (marketDataMap[ticker]) updateKeyStats(marketDataMap[ticker]);
    // 2. 차트 로드
    await loadChart(ticker);
}

async function loadChart(ticker) {
    if (!els.chartContainer) return;
    
    if (chart) { chart.remove(); chart = null; }
    els.chartContainer.innerHTML = ''; 
    
    // 오버레이 복구
    const overlayHTML = `
        <div class="chart-overlay" style="position:absolute; top:12px; left:12px; z-index:10; display:flex; gap:10px; align-items:baseline; pointer-events:none;">
            <span id="overlay-ticker" style="font-size:20px; font-weight:900; color:#000;">${ticker}</span>
            <span id="overlay-price" style="font-family:'JetBrains Mono'; font-size:18px; font-weight:600; color:#34C759;">Loading...</span>
        </div>`;
    els.chartContainer.insertAdjacentHTML('afterbegin', overlayHTML);
    els.overlayTicker = document.getElementById('overlay-ticker');
    els.overlayPrice = document.getElementById('overlay-price');

    chart = createChart(els.chartContainer, {
        width: els.chartContainer.clientWidth,
        height: els.chartContainer.clientHeight || 350,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
        rightPriceScale: { borderColor: '#e1e1e1' },
        timeScale: { borderColor: '#e1e1e1', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 1 } 
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#34C759', downColor: '#FF3B30', borderVisible: false, wickUpColor: '#34C759', wickDownColor: '#ff3b30'
    });

    try {
        // 실제 데이터 연동 (API가 없으면 더미 데이터 사용)
        const res = await fetch(`/api/chart_data/${ticker}`);
        if(res.ok) {
            const json = await res.json();
            if(json.status === 'OK') candleSeries.setData(json.results);
        } else {
            // Fallback Dummy Data (데모용)
            candleSeries.setData(generateDummyData());
        }
        chart.timeScale().fitContent();
        
        window.addEventListener('resize', () => {
            if(chart) chart.applyOptions({ width: els.chartContainer.clientWidth, height: els.chartContainer.clientHeight });
        });
    } catch(e) { console.error(e); }
}

function generateDummyData() {
    let res = [];
    let time = Math.floor(Date.now() / 1000) - (200 * 60);
    let close = 100 + Math.random() * 10;
    for(let i=0; i<200; i++) {
        let open = close;
        let change = (Math.random() - 0.5) * (open * 0.01);
        close = open + change;
        let high = Math.max(open, close) + Math.random() * 0.1;
        let low = Math.min(open, close) - Math.random() * 0.1;
        res.push({ time, open, high, low, close });
        time += 60;
    }
    return res;
}

// ==========================================================================
// PART 4. INIT & FCM
// ==========================================================================

setInterval(updateDashboard, 1000); 
updateDashboard();

document.addEventListener('DOMContentLoaded', () => {
    const subBtn = document.getElementById('subscribe-btn');
    if (subBtn) subBtn.addEventListener('click', requestNotificationPermission);
    
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(console.error);
    }
});

async function requestNotificationPermission() {
    const permission = await Notification.requestPermission();
    if (permission === 'granted') getFCMToken();
}

async function getFCMToken() {
    try {
        const vapidKey = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
        const token = await getToken(messaging, { vapidKey });
        if (token) {
            fetch("/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
            alert("✅ Alerts Enabled!");
        }
    } catch(e) { console.error(e); }
}