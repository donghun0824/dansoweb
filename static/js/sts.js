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
let marketDataMap = {}; // Stores real-time data for quick access

// Map HTML IDs from your Webull-style layout
const els = {
    scannerList: document.getElementById('ticker-list-container'),
    chartContainer: document.getElementById('chart-container'),
    signals: document.getElementById('signal-feed-container'),
    
    // Status Bar
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

        // 1. Store data mapping for instant access on click
        data.targets.forEach(t => {
            marketDataMap[t.ticker] = t;
        });

        // 2. Render Left Scanner List
        renderScannerList(data.targets);
        
        // 3. Auto-select first ticker if none selected
        if (!currentTicker && data.targets.length > 0) {
            selectTicker(data.targets[0].ticker);
        }
        
        // 4. Update Bottom Panel for current ticker
        if (currentTicker && marketDataMap[currentTicker]) {
            updateKeyStats(marketDataMap[currentTicker]);
        }

        // 5. Update Status Text
        if(els.statusText) els.statusText.innerText = "Active (V9.3)";
        if(els.countText) els.countText.innerText = `${data.targets.length} Targets`;

        // 6. Render Signals Log
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
        const score = Math.round(item.ai_score || 0);
        const price = parseFloat(item.price).toFixed(2);
        // Highlight active ticker
        const isActive = (item.ticker === currentTicker) ? 'background:rgba(0,122,255,0.1); border-left:3px solid #007AFF;' : '';

        const html = `
            <div class="ticker-row" style="${isActive}; cursor:pointer; padding:10px 12px; border-bottom:1px solid #eee; transition: background 0.2s;" onclick="selectTicker('${item.ticker}')">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <div class="t-sym" style="font-weight:800; font-size:14px; color:#1D1D1F;">${item.ticker}</div>
                        <div class="t-sub" style="font-size:10px; color:#86868B;">Score <span style="font-weight:bold; color:${score>=80?'#007AFF':'#333'}">${score}</span></div>
                    </div>
                    <div class="t-price" style="font-family:'JetBrains Mono'; font-weight:600; font-size:13px; color:#1D1D1F;">$${price}</div>
                </div>
            </div>`;
        els.scannerList.insertAdjacentHTML('beforeend', html);
    });
}

// 🔥 [CORE] Populate Webull-style Bottom Panel
function updateKeyStats(data) {
    // 1. Update Chart Overlay
    if(els.overlayTicker) els.overlayTicker.innerText = data.ticker;
    if(els.overlayPrice) els.overlayPrice.innerText = `$${parseFloat(data.price).toFixed(2)}`;

    // 2. Helper formatters
    const fmt = (val, fixed=2) => val !== undefined && val !== null ? parseFloat(val).toFixed(fixed) : '--';
    const color = (val) => parseFloat(val) > 0 ? '#34C759' : (parseFloat(val) < 0 ? '#FF3B30' : '#333');

    // 3. Inject Data into Grid Cells
    if(els.indObi) { els.indObi.innerText = fmt(data.obi); els.indObi.style.color = color(data.obi); }
    if(els.indObiMom) { els.indObiMom.innerText = fmt(data.obi_mom); els.indObiMom.style.color = color(data.obi_mom); }
    
    if(els.indVpin) { 
        els.indVpin.innerText = fmt(data.vpin); 
        els.indVpin.style.color = parseFloat(data.vpin) > 0.8 ? '#FF3B30' : '#333'; 
    }
    
    if(els.indTickSpeed) els.indTickSpeed.innerText = data.tick_speed || '0';
    if(els.indTickAccel) { els.indTickAccel.innerText = fmt(data.tick_accel, 1); els.indTickAccel.style.color = color(data.tick_accel); }
    
    if(els.indVwapDist) { els.indVwapDist.innerText = fmt(data.vwap_dist) + '%'; els.indVwapDist.style.color = color(data.vwap_dist); }
    if(els.indVwapSlope) { els.indVwapSlope.innerText = fmt(data.vwap_slope, 1); els.indVwapSlope.style.color = color(data.vwap_slope); }
    if(els.indSqueeze) els.indSqueeze.innerText = fmt(data.squeeze_ratio);
    if(els.indRvol) { 
        els.indRvol.innerText = fmt(data.rvol, 1) + 'x'; 
        els.indRvol.style.fontWeight = parseFloat(data.rvol) > 3 ? '800' : '400'; 
    }
    if(els.indAtr) els.indAtr.innerText = fmt(data.atr, 3);
    
    if(els.indPumpAccel) { els.indPumpAccel.innerText = fmt(data.pump_accel) + '%'; els.indPumpAccel.style.color = color(data.pump_accel); }
    if(els.indSpread) els.indSpread.innerText = fmt(data.spread) + '%';
    
    if(els.indScore) {
        const s = Math.round(data.ai_score || 0);
        els.indScore.innerText = s;
        els.indScore.style.color = s >= 80 ? '#007AFF' : '#333'; // Blue for Elite
        
        // Calculate Win Probability based on score
        if(els.indProb) els.indProb.innerText = s >= 60 ? `${Math.min(99, Math.round(s*0.95))}%` : '--';
    }
    
    if(els.indTimestamp) els.indTimestamp.innerText = new Date().toLocaleTimeString();
}

function renderSignals(logs) {
    if (!els.signals) return;
    els.signals.innerHTML = '';
    logs.forEach(log => {
        const html = `
            <div style="padding:10px; border-bottom:1px solid #eee; transition: background 0.2s;">
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span style="background:rgba(52, 199, 89, 0.15); color:#34C759; padding:2px 6px; border-radius:4px; font-size:9px; font-weight:bold;">BUY</span>
                    <span style="font-size:10px; color:#999;">${log.timestamp.split(' ')[1] || log.timestamp}</span>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:bold; color:#1D1D1F;">${log.ticker}</span>
                    <span style="font-family:'JetBrains Mono'; font-size:13px;">$${parseFloat(log.price).toFixed(2)}</span>
                </div>
            </div>`;
        els.signals.insertAdjacentHTML('beforeend', html);
    });
}

/* ==========================================================================
   PART 3. CHART ENGINE
   ========================================================================== */

// Make accessible to HTML
window.selectTicker = async function(ticker) {
    currentTicker = ticker;
    // 1. Instant update if data exists
    if (marketDataMap[ticker]) updateKeyStats(marketDataMap[ticker]);
    // 2. Load chart
    await loadChart(ticker);
}

async function loadChart(ticker) {
    if (!els.chartContainer) return;
    
    // Reset Chart
    if (chart) { chart.remove(); chart = null; }
    els.chartContainer.innerHTML = ''; 
    
    // Restore Overlay
    const overlayHTML = `
        <div class="chart-overlay" style="position:absolute; top:12px; left:16px; z-index:10; display:flex; gap:10px; align-items:baseline; pointer-events:none;">
            <span id="overlay-ticker" style="font-size:20px; font-weight:900; letter-spacing:-0.5px; color:#000;">${ticker}</span>
            <span id="overlay-price" style="font-family:'JetBrains Mono'; font-size:18px; font-weight:600; color:#34C759;">Loading...</span>
        </div>`;
    els.chartContainer.insertAdjacentHTML('afterbegin', overlayHTML);
    els.overlayTicker = document.getElementById('overlay-ticker');
    els.overlayPrice = document.getElementById('overlay-price');

    // Create Chart
    chart = createChart(els.chartContainer, {
        width: els.chartContainer.clientWidth,
        height: els.chartContainer.clientHeight || 350,
        layout: { background: { color: '#ffffff' }, textColor: '#333' },
        grid: { vertLines: { color: 'rgba(0,0,0,0.05)' }, horzLines: { color: 'rgba(0,0,0,0.05)' } },
        rightPriceScale: { borderColor: '#e1e1e1' },
        timeScale: { borderColor: '#e1e1e1', timeVisible: true, secondsVisible: false },
        crosshair: { mode: 1 } 
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#34C759', downColor: '#FF3B30', borderVisible: false, wickUpColor: '#34C759', wickDownColor: '#ff3b30'
    });

    try {
        const res = await fetch(`/api/chart_data/${ticker}`);
        if(res.ok) {
            const json = await res.json();
            if(json.status === 'OK') {
                candleSeries.setData(json.results);
            }
        } else {
            // Fallback for demo
            candleSeries.setData(generateDummyData());
        }
        chart.timeScale().fitContent();
        
        // Responsive resize
        const resizeObserver = new ResizeObserver(entries => {
            if (entries.length === 0 || entries[0].target !== els.chartContainer) { return; }
            const newRect = entries[0].contentRect;
            chart.applyOptions({ width: newRect.width, height: newRect.height });
        });
        resizeObserver.observe(els.chartContainer);
        
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

setInterval(updateDashboard, 1000); // 1-second polling
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