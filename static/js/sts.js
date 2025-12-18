import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

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
   [수정 1] SOCKET CONNECTION (여기에 추가하세요)
   ========================================================================== */
// HTML에서 socket.io CDN을 불러왔는지 확인하고 연결합니다.
let socket = null;
if (typeof io !== 'undefined') {
    // 나중에 구글 로그인 연동 시 userInfo 값을 실제 로그인 정보로 바꾸면 됩니다.
    const userInfo = {
        name: "Trader",  
        email: "guest@danso.ai"
    };

    // 서버로 연결 시도 (이름표 달고 입장)
    socket = io({
        query: {
            username: userInfo.name,
            email: userInfo.email
        }
    });

    console.log("🔌 Socket Initialized for Chat");
} else {
    console.warn("⚠️ Socket.io not found. Chat will be offline.");
}

/* ==========================================================================
   PART 1. GLOBAL STATE & DOM ELEMENTS
   ========================================================================== */
let chart = null;
let candleSeries = null;
let currentTicker = null;
let marketDataMap = {}; // Stores real-time data for quick access

// Map HTML IDs from your Webull-style layout
// Map HTML IDs (Terminal UI Version)
const els = {
    // 1. 공통 요소 (스캐너, 차트, 시그널)
    scannerList: document.getElementById('ticker-list-container'),
    chartContainer: document.getElementById('chart-container'),
    signals: document.getElementById('signal-feed-container'),
    
    // 2. 상단 상태바 & 차트 오버레이
    statusText: document.getElementById('scan-status-text'),
    countText: document.getElementById('scan-watching-count'),
    overlayTicker: document.getElementById('overlay-ticker'),
    overlayPrice: document.getElementById('overlay-price'),
    
    // 3. [터미널 UI] 텍스트 값 (Text Values)
    indScore: document.getElementById('ind-score'),
    indProb: document.getElementById('ind-prob'),
    indOfi: document.getElementById('ind-ofi'),
    indBook: document.getElementById('ind-book'),
    indLiq1m: document.getElementById('ind-liq-1m'),
    indRsi: document.getElementById('ind-rsi'),
    indRvol: document.getElementById('ind-rvol'),
    indVpin: document.getElementById('ind-vpin'),

    // 4. [터미널 UI] 게이지 바 (Gauge Bars)
    barScore: document.getElementById('bar-score'),
    barProb: document.getElementById('bar-prob'),
    barOfi: document.getElementById('bar-ofi'),
    barBook: document.getElementById('bar-book'),
    barLiq1m: document.getElementById('bar-liq-1m'),
    barRsi: document.getElementById('bar-rsi'),
    barRvol: document.getElementById('bar-rvol'),
    barVpin: document.getElementById('bar-vpin'),
    
    // 5. [팝업] 설명창 (Modal)
    modal: document.getElementById('info-modal'),
    modalTerm: document.getElementById('modal-term'),
    modalKr: document.getElementById('modal-desc-kr'),
    modalEn: document.getElementById('modal-desc-en'),
};

/* ==========================================================================
   PART 2. DATA POLLING & RENDERING (수정됨: V9.3 UI + V7.1 방어 로직)
   ========================================================================== */
// [FIX] 값이 0이어도 숫자를 표시하고, 진짜 없을 때만 '--' 표시하는 함수
function formatMetric(value, decimals = 2) {
    if (value === null || value === undefined || isNaN(value)) {
        return '<span style="color:#ccc;">--</span>'; // 값 없으면 회색 --
    }
    return Number(value).toFixed(decimals); // 0.00 등 숫자 정상 표시
}

async function updateDashboard() {
    // console.log("🔄 Fetching STS Status..."); 

    try {
        const res = await fetch('/api/sts/status');
        
        if (!res.ok) {
            console.error(`📡 API Error: ${res.status}`);
            return;
        }
        
        let data = await res.json();
        
        // 데이터 구조 방어 로직
        if (!data) data = { targets: [], logs: [] };
        if (!data.targets) data.targets = [];

        // 1. Store data mapping
        data.targets.forEach(t => {
            marketDataMap[t.ticker] = t;
        });

        // 2. Render Scanner List
        renderScannerList(data.targets);
        
        // 3. Auto-select logic
        if (!currentTicker && data.targets.length > 0) {
            selectTicker(data.targets[0].ticker);
        }
        
        // 4. Update Bottom Panel
        if (currentTicker && marketDataMap[currentTicker]) {
            updateKeyStats(marketDataMap[currentTicker]);
        }

        // 5. Update Status Text
        if(els.statusText) els.statusText.innerText = "Active (STS Engine)";
        if(els.countText) els.countText.innerText = `${data.targets.length} Targets`;

        // [수정 완료] '가짜 자동 생성' 로직 삭제함. 
        // 오직 서버 DB(signals 테이블)에 저장된 '진짜 매수 체결' 내역만 가져옵니다.
        const finalLogs = data.logs || [];

        // 6. Render Signals Log
        renderSignals(finalLogs);
        
        // ▲▲▲ [여기까지] ▲▲▲

    } catch (e) {
        console.error("🚨 Dashboard Sync Error:", e);
    }
}

    

function renderScannerList(targets) {
    if (!els.scannerList) return;
    els.scannerList.innerHTML = '';

    // 타겟이 0개일 때 대기 화면 표시
    if (targets.length === 0) {
        els.scannerList.innerHTML = `
            <div style="padding:40px 20px; text-align:center; color:#86868B;">
                <div style="margin-bottom:10px; font-size:18px;">📡</div>
                <div>Scanning Markets...</div>
                <div style="font-size:11px; margin-top:5px; opacity:0.6;">Engine is running</div>
            </div>`;
        return;
    }

    // 4. 타겟 목록 렌더링 루프
    targets.forEach(item => {
        // --- [A] 점수 계산 및 포맷팅 ---
        // 0.xx 확률값이면 100을 곱해서 점수로 변환
        let rawScore = item.ai_score !== undefined ? item.ai_score : (item.ai_prob || 0);
        if (rawScore <= 1 && rawScore > 0) rawScore *= 100;
        const score = Math.round(rawScore);

        // --- [B] 가격 포맷팅 ---
        const priceVal = item.price ? parseFloat(item.price) : 0;
        const priceStr = priceVal.toFixed(2);

        // --- [C] 등락률 계산 및 색상 결정 (핵심 수정 사항) ---
        // 백엔드에서 'day_change' 혹은 'change'로 들어오는 값을 받음
        const chgVal = parseFloat(item.change || item.day_change || 0);
        
        // 부호 처리 (+ 기호 붙이기)
        const sign = chgVal > 0 ? '+' : '';
        const chgStr = `${sign}${chgVal.toFixed(2)}%`;
        
        // CSS 클래스 결정 (CSS에 정의된 .up, .down, .flat 사용)
        let chgClass = 'flat';
        if (chgVal > 0) chgClass = 'up';     // 양수: 초록
        if (chgVal < 0) chgClass = 'down';   // 음수: 빨강

        // --- [D] 상태 클래스 (고득점, 선택됨) ---
        const isHighScore = score >= 80;
        const activeClass = (item.ticker === currentTicker) ? 'active' : '';
        const highScoreClass = isHighScore ? 'high-score' : '';

        // --- [E] HTML 조립 (배지 적용됨) ---
        const html = `
            <div class="ticker-row ${highScoreClass} ${activeClass}" onclick="selectTicker('${item.ticker}')">
                
                <div class="ticker-left">
                    <div class="t-symbol">${item.ticker}</div>
                    <div class="t-score-badge">Score ${score}</div>
                </div>

                <div class="ticker-right">
                    <div class="t-price">$${priceStr}</div>
                    <div class="t-change-badge ${chgClass}">
                        ${chgStr}
                    </div>
                </div>

            </div>`;
            
        els.scannerList.insertAdjacentHTML('beforeend', html);
    });
}
// [추가] 등락률 표시 헬퍼 (데이터에 change가 있다면 표시)
function renderMiniChange(item) {
    if (!item.change && !item.day_change) return '';
    const chg = item.change || item.day_change;
    const color = chg > 0 ? '#34C759' : (chg < 0 ? '#FF3B30' : '#86868B');
    return `<div style="font-size:10px; font-weight:500; color:${color};">${chg > 0 ? '+' : ''}${parseFloat(chg).toFixed(2)}%</div>`;
}

// [sts.js] updateKeyStats 함수 전체 교체

function updateKeyStats(data) {
    if (!data) return;

    // [Helper 1] 값 포맷터
    const fmt = (val, fixed=2) => {
        if (val === undefined || val === null || val === '') return '--';
        const num = parseFloat(val);
        if (isNaN(num)) return '--';
        return num.toFixed(fixed);
    };

    // [Helper 2] 색상 처리 (양수:초록, 음수:빨강)
    const color = (val) => {
        const v = parseFloat(val);
        if (isNaN(v)) return '#333';
        return v > 0 ? '#00C076' : (v < 0 ? '#FF3B30' : '#86868B');
    };

    // -------------------------------------------------------
    // 1. 텍스트 데이터 업데이트 (기존 로직 유지)
    // -------------------------------------------------------
    
    // 상단 오버레이
    if(els.overlayTicker) els.overlayTicker.innerText = data.ticker || "WAITING";
    if(els.overlayPrice) {
        els.overlayPrice.innerText = `$${fmt(data.price)}`;
        if(data.day_change) els.overlayPrice.style.color = color(data.day_change);
    }

    // TIER 1: SCORE
    if(els.indScore) {
        let s = data.ai_score ?? data.score ?? 0;
        if (s <= 1 && s > 0) s *= 100; // 0.85 -> 85 변환
        els.indScore.innerText = Math.round(s);
        els.indScore.style.color = s >= 80 ? '#007AFF' : (s >= 50 ? '#FF9500' : '#333');
    }
    if(els.indProb) {
        let p = data.ai_score ?? 0;
        if (p <= 1 && p > 0) p *= 100;
        els.indProb.innerText = p > 0 ? `${Math.round(p)}%` : '--%';
    }

    // TIER 2: MONEY FLOW (텍스트)
    if(els.indOfi) { els.indOfi.innerText = fmt(data.ofi, 2); els.indOfi.style.color = color(data.ofi); }
    if(els.indBook) {
        const val = parseFloat(data.top5_book_usd || 0);
        let text = val >= 1000000 ? (val/1000000).toFixed(1)+'M' : (val/1000).toFixed(0)+'K';
        els.indBook.innerText = '$' + text;
        els.indBook.style.color = val >= 100000 ? '#00C076' : (val < 40000 ? '#FF3B30' : '#888');
    }
    if(els.indLiq1m) {
        const val = parseFloat(data.dollar_vol_1m || 0);
        let text = val >= 1000000 ? (val/1000000).toFixed(1)+'M' : (val/1000).toFixed(0)+'K';
        els.indLiq1m.innerText = '$' + text;
    }

    // TIER 3: TECHNICALS (텍스트)
    if(els.indRsi) { els.indRsi.innerText = fmt(data.rsi, 1); }
    if(els.indRvol) { els.indRvol.innerText = fmt(data.rvol, 1) + 'x'; }
    if(els.indVpin) { els.indVpin.innerText = fmt(data.vpin, 2); }


    // -------------------------------------------------------
    // 🔥 [NEW] 게이지 바 시각화 로직 (여기서부터 추가됨)
    // -------------------------------------------------------

    // 1. AI Score Bar (0~100)
    if(els.barScore) {
        let s = data.ai_score ?? 0;
        if (s <= 1 && s > 0) s *= 100;
        els.barScore.style.width = `${Math.min(100, Math.max(0, s))}%`;
        // 색상: 80이상 파랑, 50이상 주황, 나머지 회색
        els.barScore.style.background = s >= 80 ? '#007AFF' : (s >= 50 ? '#FF9500' : '#333');
    }
    
    // 2. Win Prob Bar
    if(els.barProb) {
        let p = data.ai_score ?? 0;
        if (p <= 1 && p > 0) p *= 100;
        els.barProb.style.width = `${Math.min(100, p)}%`;
        els.barProb.style.background = '#5856D6'; // 보라색
    }

    // 3. OFI Bar (중앙 기준, 핵심!)
    if(els.barOfi) {
        const ofi = parseFloat(data.ofi || 0);
        // 최대치 설정을 ±5000 정도로 잡음 (상황에 따라 조절)
        const MAX_OFI = 2000; 
        let pct = (ofi / MAX_OFI) * 50; // 절반(50%) 기준 비율 계산
        pct = Math.min(50, Math.max(-50, pct)); // ±50% 넘지 않게 제한
        
        if (pct >= 0) {
            // 양수: 중앙(50%)에서 오른쪽으로
            els.barOfi.style.left = '50%';
            els.barOfi.style.width = `${pct}%`;
            els.barOfi.style.background = '#00ff9d'; // Green
        } else {
            // 음수: 중앙에서 왼쪽으로 (width는 양수여야 함)
            els.barOfi.style.left = `${50 + pct}%`; 
            els.barOfi.style.width = `${Math.abs(pct)}%`;
            els.barOfi.style.background = '#ff3b30'; // Red
        }
    }

    // 4. Book Depth Bar ($500k 기준)
    if(els.barBook) {
        const val = parseFloat(data.top5_book_usd || 0);
        const MAX_BOOK = 500000; // 50만불이면 꽉 참
        const fill = Math.min(100, (val / MAX_BOOK) * 100);
        els.barBook.style.width = `${fill}%`;
        // $100k 이상이면 안전(초록), 아니면 위험(빨강)
        els.barBook.style.background = val >= 100000 ? '#00C076' : '#FF3B30';
    }

    // 5. Vol 1M Bar ($2M 기준)
    if(els.barLiq1m) {
        const val = parseFloat(data.dollar_vol_1m || 0);
        const MAX_VOL = 2000000; // 200만불이면 꽉 참
        const fill = Math.min(100, (val / MAX_VOL) * 100);
        els.barLiq1m.style.width = `${fill}%`;
        els.barLiq1m.style.background = '#007AFF';
    }

    // 6. RSI Bar (0~100)
    if(els.barRsi) {
        const rsi = parseFloat(data.rsi || 50);
        els.barRsi.style.width = `${rsi}%`;
        // 과매도(<30):초록, 과매수(>70):빨강, 중립:회색
        if(rsi <= 30) els.barRsi.style.background = '#00ff9d';
        else if(rsi >= 70) els.barRsi.style.background = '#ff3b30';
        else els.barRsi.style.background = '#555';
    }

    // 7. RVOL Bar (0~5배)
    if(els.barRvol) {
        const rvol = parseFloat(data.rvol || 0);
        const fill = Math.min(100, (rvol / 5.0) * 100);
        els.barRvol.style.width = `${fill}%`;
        // 3배 이상이면 보라색(폭발), 아니면 파란색
        els.barRvol.style.background = rvol >= 3.0 ? '#AF52DE' : '#007AFF';
    }

    // 8. VPIN Bar (0~1.0)
    if(els.barVpin) {
        const vpin = parseFloat(data.vpin || 0);
        const fill = Math.min(100, vpin * 100); // 1.0이면 100%
        els.barVpin.style.width = `${fill}%`;
        // 높을수록 위험(빨강)
        els.barVpin.style.background = '#ff3b30';
    }
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

/* ==========================================================================
   PART 4. INIT & REAL-TIME CHAT (Socket.io) - [수정됨]
   ========================================================================== */
setInterval(updateDashboard, 1000); 
updateDashboard();

document.addEventListener('DOMContentLoaded', () => {
    // ------------------------------------------------------------
    // 1. 기존 알림 구독 로직 유지
    // ------------------------------------------------------------
    const subBtn = document.getElementById('subscribe-btn');
    if (subBtn) subBtn.addEventListener('click', requestNotificationPermission);
    
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(console.error);
    }

    // ------------------------------------------------------------
    // 2. [수정됨] 채팅 로직 (봇 메시지 + 유저 대화)
    // ------------------------------------------------------------
    const chatInput = document.querySelector('.chat-input'); 
    const chatBtn = document.getElementById('post-submit-btn');
    const chatBody = document.getElementById('community-feed-container');

    // [A] 메시지 수신 (서버에서 봇이나 다른 사람의 글이 왔을 때)
    if (socket) {
        socket.on('chat_message', (data) => {
            if (!chatBody) return;
            
            // 봇인지 확인 (type이 bot_signal이면 봇)
            const isBot = data.type === 'bot_signal' || data.type === 'bot_welcome';
            const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            let html = '';

            // 🤖 [봇] 파란색 강조 박스 디자인
            if (isBot) {
                html = `
                    <div style="margin-bottom:12px; display:flex; gap:10px; opacity:0; animation:fadeIn 0.3s forwards;">
                        <div style="width:32px; height:32px; border-radius:50%; background:#007AFF; color:white; display:flex; align-items:center; justify-content:center; flex-shrink:0; font-size:16px;">🤖</div>
                        <div style="background:rgba(0, 113, 227, 0.08); border:1px solid rgba(0, 113, 227, 0.2); padding:10px; border-radius:12px; font-size:13px; width:100%;">
                            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                <strong style="color:#007AFF;">${data.user}</strong>
                                <span style="font-size:10px; color:#999;">${time}</span>
                            </div>
                            <div style="color:#333; line-height:1.4;">${data.message}</div>
                        </div>
                    </div>`;
            } 
            // 👤 [사람] 일반 말풍선 디자인
            else {
                const isMe = data.user === "Trader"; // 내 이름이면 오른쪽 정렬
                const alignStyle = isMe ? 'justify-content:flex-end;' : 'justify-content:flex-start;';
                const bgStyle = isMe ? 'background:#007AFF; color:white; border-radius:12px 12px 0 12px;' : 'background:#f5f5f7; color:#333; border-radius:12px 12px 12px 0;';
                
                html = `
                    <div style="display:flex; ${alignStyle} margin-bottom:10px;">
                        <div style="max-width:85%;">
                            ${!isMe ? `<div style="font-size:11px; color:#999; margin-bottom:2px;">${data.user}</div>` : ''}
                            <div style="${bgStyle} padding:8px 12px; font-size:13px; display:inline-block; text-align:left;">
                                ${data.message}
                            </div>
                            <div style="font-size:10px; color:#ccc; margin-top:2px; text-align:${isMe?'right':'left'};">${time}</div>
                        </div>
                    </div>`;
            }

            chatBody.insertAdjacentHTML('beforeend', html);
            chatBody.scrollTop = chatBody.scrollHeight; // 스크롤 하단 고정
        });
    }

    // [B] 메시지 전송 (내가 글 쓸 때)
    function sendMsg() {
        if (!chatInput || !chatInput.value.trim()) return;
        if (!socket) { alert("채팅 연결이 끊겨있습니다."); return; }

        const msg = chatInput.value.trim();
        
        // 서버로 전송 (화면에 그리는 건 위 [A]에서 처리함)
        socket.emit('send_message', { 
            user: "Trader", 
            message: msg,
            type: 'user' 
        });

        chatInput.value = ''; // 입력창 비우기
    }

    // 이벤트 리스너 연결
    if (chatBtn) {
        chatBtn.addEventListener('click', (e) => { e.preventDefault(); sendMsg(); });
    }
    if (chatInput) {
        chatInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); sendMsg(); }
        });
    }
    
    // 애니메이션 스타일 추가
    const style = document.createElement('style');
    style.innerHTML = `@keyframes fadeIn { from { opacity:0; transform:translateY(5px); } to { opacity:1; transform:translateY(0); } }`;
    document.head.appendChild(style);
});

async function requestNotificationPermission() {
    const permission = await Notification.requestPermission();
    if (permission === 'granted') getFCMToken();
}

async function getFCMToken() {
    try {
        const registration = await navigator.serviceWorker.ready;
        const vapidKey = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
        const token = await getToken(messaging, { vapidKey: vapidKey, serviceWorkerRegistration: registration });

        if (token) {
            // [수정됨] /subscribe -> /api/register_token
            await fetch("/api/register_token", { 
                method: "POST", 
                headers: { "Content-Type": "application/json" }, 
                body: JSON.stringify({ token }) 
            });
            console.log("📱 Token sent to server:", token);
            alert("✅ Alerts Enabled! (Real-time notifications active)");
       }
    } catch(e) { console.error("🚨 FCM Token Error:", e); }
}
// ==========================================
// [POPUP] 지표 설명 데이터 및 함수
// ==========================================
const METRIC_DICT = {
    'SCORE': {
        kr: "AI와 퀀트 모델이 분석한 종합 점수입니다. 80점 이상이면 강력한 매수 신호입니다.",
        en: "Comprehensive score by AI & Quant models. >80 indicates a strong buy signal."
    },
    'PROB': {
        kr: "과거 패턴 학습을 통해 예측한 상승 확률입니다. 높을수록 신뢰도가 높습니다.",
        en: "Predicted win probability based on historical patterns."
    },
    'OFI': {
        kr: "주문 흐름 불균형(Order Flow Imbalance). 양수(초록)면 공격적 매수세, 음수(빨강)면 매도세입니다.",
        en: "Order Flow Imbalance. Positive values indicate aggressive buying pressure."
    },
    'BOOK': {
        kr: "상위 5호가에 쌓인 매수 잔량 총액입니다. 호가가 두터워야 가격이 쉽게 밀리지 않습니다.",
        en: "Total value of top 5 bid orders. Thicker books prevent slippage."
    },
    'VOL': {
        kr: "최근 1분간 체결된 거래대금입니다. 유동성이 공급되어야 급등이 가능합니다.",
        en: "Dollar volume traded in the last minute. Liquidity fuels momentum."
    },
    'RSI': {
        kr: "상대강도지수. 70 이상은 과매수, 30 이하는 과매도 구간입니다.",
        en: "Relative Strength Index. >70 Overbought, <30 Oversold."
    },
    'RVOL': {
        kr: "상대 거래량. 평소 대비 거래량이 몇 배 터졌는지 보여줍니다. 3.0x 이상이면 폭발적입니다.",
        en: "Relative Volume. Shows how many times current volume exceeds the average."
    },
    'VPIN': {
        kr: "주문 독성(Toxicity). 수치가 높으면(>1.0) 정보 비대칭이 심해 급락 위험이 큽니다.",
        en: "Volume-Synchronized Probability of Informed Trading. High values indicate toxic flow."
    }
};

window.showInfo = function(key) {
    if(!els.modal) return;
    const info = METRIC_DICT[key];
    if(info) {
        els.modalTerm.innerText = key;
        els.modalKr.innerText = info.kr;
        els.modalEn.innerText = info.en;
        els.modal.style.display = 'block';
    }
}
window.closeInfo = function() {
    if(els.modal) els.modal.style.display = 'none';
}