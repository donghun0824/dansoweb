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
    indProb: document.getElementById('ind-prob'),
    // 🔥 [NEW] 여기에 새로 만든 지표 ID 추가
    indRsi: document.getElementById('ind-rsi'),
    indStoch: document.getElementById('ind-stoch'),
    indFibo: document.getElementById('ind-fibo'),
    indObiRev: document.getElementById('ind-obi-rev'),
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

function updateKeyStats(data) {
    if (!data) return;

    // [Helper 1] 값 포맷터
    const fmt = (val, fixed=2) => {
        if (val === undefined || val === null || val === '') return '--';
        const num = parseFloat(val);
        if (isNaN(num)) return '--';
        return num.toFixed(fixed);
    };

    // [Helper 2] 색상 처리 (양수:초록, 음수:빨강, 0:검정)
    const color = (val) => {
        const v = parseFloat(val);
        if (isNaN(v)) return '#333';
        return v > 0 ? '#00C076' : (v < 0 ? '#FF3B30' : '#333');
    };

    // [Helper 3] RSI용 색상 (낮을수록 좋음 = 초록색 / 높으면 과열 = 빨강)
    const getRsiColor = (val) => {
        if (val <= 30) return '#00C076'; // 과매도(매수 기회)
        if (val >= 70) return '#FF3B30'; // 과매수(과열)
        return '#333';
    };

    // -------------------------------------------------------
    // 1. 상단 오버레이 (티커/가격)
    // -------------------------------------------------------
    if(els.overlayTicker) els.overlayTicker.innerText = data.ticker || "WAITING";
    if(els.overlayPrice) {
        els.overlayPrice.innerText = `$${fmt(data.price)}`;
        if(data.day_change) els.overlayPrice.style.color = color(data.day_change);
    }

    // -------------------------------------------------------
    // 2. 핵심 지표 매핑 (백엔드 키 -> 화면)
    // -------------------------------------------------------

    // OBI
    if(els.indObi) { 
        els.indObi.innerText = fmt(data.obi); 
        els.indObi.style.color = color(data.obi); 
    }
    // OBI MOM
    if(els.indObiMom) { 
        const val = data.obi_mom ?? data.obi_momentum ?? 0;
        els.indObiMom.innerText = fmt(val); 
        els.indObiMom.style.color = color(val); 
    }
    // VPIN
    if(els.indVpin) { 
        els.indVpin.innerText = fmt(data.vpin); 
        els.indVpin.style.color = parseFloat(data.vpin) > 0.8 ? '#FF3B30' : '#333'; 
        els.indVpin.style.fontWeight = parseFloat(data.vpin) > 0.8 ? '800' : '400';
    }
    // Tick Speed & Accel
    if(els.indTickSpeed) els.indTickSpeed.innerText = data.tick_speed || '0';
    if(els.indTickAccel) { 
        els.indTickAccel.innerText = fmt(data.tick_accel, 1); 
        els.indTickAccel.style.color = color(data.tick_accel); 
    }
    // VWAP Dist & Slope
    if(els.indVwapDist) { 
        els.indVwapDist.innerText = fmt(data.vwap_dist) + '%'; 
        els.indVwapDist.style.color = color(data.vwap_dist); 
    }
    if(els.indVwapSlope) { 
        els.indVwapSlope.innerText = fmt(data.vwap_slope, 2); 
        els.indVwapSlope.style.color = color(data.vwap_slope); 
    }
    // Squeeze
    if(els.indSqueeze) {
        const sqz = data.squeeze_ratio ?? data.squeeze ?? 0;
        els.indSqueeze.innerText = fmt(sqz);
        els.indSqueeze.style.color = parseFloat(sqz) < 0.8 ? '#FF3B30' : '#333';
        els.indSqueeze.style.fontWeight = parseFloat(sqz) < 0.8 ? '800' : '400';
    }
    // RVOL
    if(els.indRvol) { 
        els.indRvol.innerText = fmt(data.rvol, 1) + 'x'; 
        const rvolVal = parseFloat(data.rvol);
        els.indRvol.style.color = rvolVal > 3.0 ? '#007AFF' : '#333';
        els.indRvol.style.fontWeight = rvolVal > 3.0 ? '800' : '400';
    }
    // ATR
    if(els.indAtr) els.indAtr.innerText = fmt(data.atr, 3);
    
    // -------------------------------------------------------
    // 🔥 [NEW] 신규 반등 지표 업데이트 (여기 추가!)
    // -------------------------------------------------------

    

    // 1. RSI
    if(els.indRsi) {
        els.indRsi.innerText = fmt(data.rsi, 1);
        els.indRsi.style.color = getRsiColor(data.rsi);
        els.indRsi.style.fontWeight = data.rsi <= 30 ? '800' : '600';
    }

    // 2. Stochastic
    if(els.indStoch) {
        els.indStoch.innerText = fmt(data.stoch, 1);
        els.indStoch.style.color = data.stoch <= 20 ? '#00C076' : '#333';
    }

    // 3. Fibonacci
    if(els.indFibo) {
        els.indFibo.innerText = fmt(data.fibo_pos, 2);
        const f = parseFloat(data.fibo_pos);
        if (f >= 0.38 && f <= 0.62) {
            els.indFibo.style.color = '#007AFF';
            els.indFibo.style.fontWeight = '800';
        } else {
            els.indFibo.style.color = '#333';
            els.indFibo.style.fontWeight = '400';
        }
    }

    // 4. OBI Reversal 연결 (이름 불일치 해결 로직 포함)
    if(els.indObiRev) {
        // DB 컬럼명(obi_rev)과 JS 변수명(obi_reversal_flag) 둘 다 체크
        const isRev = (data.obi_rev === 1) || (data.obi_reversal_flag === 1);
        
        if (isRev) {
            els.indObiRev.innerHTML = '<span style="background:#00C076; color:white; padding:2px 4px; border-radius:4px; font-size:10px;">TURN</span>';
        } else {
            els.indObiRev.innerText = '-';
        }
    }

    // -------------------------------------------------------
    // 3. 점수 (Hybrid Score)
    // -------------------------------------------------------
    if(els.indScore) {
        let rawScore = data.ai_score ?? data.score ?? 0;
        if (rawScore <= 1 && rawScore > 0) rawScore *= 100;
        const s = Math.round(rawScore);
        
        els.indScore.innerText = s;
        els.indScore.style.color = s >= 80 ? '#007AFF' : (s >= 50 ? '#FF9500' : '#333');
        
        if(els.indProb) {
            els.indProb.innerText = s >= 1 ? `${Math.min(99, Math.round(s * 0.95))}%` : '--';
        }
    }
    
    if(els.indTimestamp) els.indTimestamp.innerText = new Date().toLocaleTimeString();
}

// [추가] 신호(Signals) 피드를 그리는 함수
function renderSignals(logs) {
    if (!els.signals) return;
    els.signals.innerHTML = '';
    
    // 로그가 없으면 리턴
    if (!logs || logs.length === 0) return;

    logs.forEach(log => {
        // 타임스탬프 처리 (시:분:초만 자르기)
        const timeStr = log.timestamp ? log.timestamp.split(' ')[1] : '--:--:--';
        
        const html = `
            <div style="padding:10px; border-bottom:1px solid rgba(0,0,0,0.05);">
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span style="background:rgba(52, 199, 89, 0.15); color:#34C759; padding:2px 6px; border-radius:4px; font-size:9px; font-weight:bold;">BUY</span>
                    <span style="font-size:10px; color:#999;">${timeStr}</span>
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
            await fetch("/subscribe", { 
                method: "POST", 
                headers: { "Content-Type": "application/json" }, 
                body: JSON.stringify({ token }) 
            });
            alert("✅ Alerts Enabled!");
       }
    } catch(e) { console.error("🚨 FCM Token Error:", e); }
}