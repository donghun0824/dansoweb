// sts.js (V7.1 - Original Logic Restored + Firebase Integration)

import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

/* ==========================================================================
   PART 0. FIREBASE & NOTIFICATION (새로 추가된 기능)
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

async function requestNotificationPermission() {
    if (!('Notification' in window)) return alert("이 브라우저는 알림을 지원하지 않습니다.");
    const permission = await Notification.requestPermission();
    if (permission === 'granted') getFCMToken();
    else alert("알림 권한이 차단되었습니다.");
}

async function getFCMToken() {
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
    
    try {
        console.log("1. Service Worker 등록 대기 중...");
        const registration = await navigator.serviceWorker.ready;
        console.log("2. Service Worker 준비 완료:", registration);

        console.log("3. 토큰 요청 시작...");
        const token = await getToken(messaging, { 
            vapidKey: VAPID_PUBLIC_KEY, 
            serviceWorkerRegistration: registration 
        });

        if (token) {
            console.log("4. 토큰 발급 성공:", token);
            window.currentFCMToken = token;
            sendTokenToServer(token);
            alert("✅ STS 알림 활성화 완료!");
        } else {
            console.log("4. 토큰이 없음 (권한 문제일 수 있음)");
        }
    } catch (err) { 
        console.error("❌ Token Error 상세:", err); 
        alert("토큰 발급 실패: " + err.message);
    }
}

function sendTokenToServer(token) {
    fetch("/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }).catch(console.error);
}

onMessage(messaging, (payload) => {
    new Notification(payload.notification.title, { body: payload.notification.body, icon: "/static/images/danso_logo.png" });
});



/* ==========================================================================
   PART 2. DATA ENGINE (기존 로직 100% 복구 + ID 매핑 수정)
   ========================================================================== */

// 맥북 스타일 HTML ID에 맞게 매핑 (기존 로직이 작동하도록 연결)
const els = {
    // clock: document.getElementById('clock'), // 맥북 UI엔 시계 없음
    tbody: document.getElementById('sts-target-table-body'), // 중앙 테이블
    // microPanel: document.getElementById('micro-panel'), // 맥북 UI엔 상세 패널이 없어서 차트 영역으로 대체 고려
    scannerList: document.getElementById('ticker-list-container'), // 좌측 리스트
    signals: document.getElementById('signal-feed-container'), // 우측 신호
    chartContainer: document.getElementById('chart-container') // 차트 영역
};

// 전역 차트 변수
let lightweightChart = null;
let candleSeries = null;
let currentModalTicker = null;

// 1. 데이터 가져오기 (기존 1.5초 Polling 유지)
async function updateDashboard() {
    try {
        const res = await fetch('/api/sts/status');
        if (!res.ok) throw new Error('Network Error');
        
        const data = await res.json();
        if (!data || !data.targets) return;

        // (A) 중앙 테이블 렌더링 (기존 로직 사용)
        renderTable(data.targets);
        
        // (B) 좌측 스캐너 리스트 렌더링 (추가됨)
        renderScannerList(data.targets);
        
        // (C) 신호 피드 렌더링 (추가됨)
        if (data.logs) renderSignals(data.logs);

        // (D) 상단 상태 텍스트
        const statusText = document.getElementById('scan-status-text');
        if (statusText) statusText.innerText = data.targets.length > 0 ? "Active" : "Idle";
        const countText = document.getElementById('scan-watching-count');
        if (countText) countText.innerText = `${data.targets.length} Targets`;

    } catch (e) {
        console.error("Dashboard Sync Error:", e);
    }
}

// 중앙 테이블 렌더링 (기존 로직: OBI, VPIN 계산 포함)
function renderTable(targets) {
    if (!els.tbody) return;
    els.tbody.innerHTML = ''; 

    if (targets.length === 0) {
        els.tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:50px; color:#8E8E93;">SCANNING MARKETS...</td></tr>`;
        return;
    }

    targets.forEach(item => {
        const price = item.price ? parseFloat(item.price).toFixed(2) : "0.00";
        let rawScore = item.ai_score || item.ai_prob || 0; 
        const scoreVal = parseFloat(rawScore);
        const displayScore = scoreVal <= 1 ? Math.round(scoreVal * 100) : scoreVal.toFixed(0);
        
        // [수정] 데이터 타입 안전성 확보 (String -> Float)
        const obi = item.obi ? parseFloat(item.obi) : 0;
        const vpin = item.vpin ? parseFloat(item.vpin) : 0;
        
        // [수정] 숫자로 변환된 변수 사용
        let riskText = 'Low';
        if (vpin > 0.6) riskText = 'Extreme';
        else if (vpin > 0.4) riskText = 'High';
        else if (vpin > 0.2) riskText = 'Med';

        const html = `
            <tr onclick="loadChartForTicker('${item.ticker}')" style="cursor:pointer;">
                <td style="font-weight:800; color:#1D1D1F;">${item.ticker}</td>
                <td style="font-family:'Roboto Mono'; font-weight:600;">$${price}</td>
                <td><span class="score-pill" style="background:${displayScore >= 80 ? 'rgba(52, 199, 89, 0.15)' : 'rgba(0, 122, 255, 0.15)'}; color:${displayScore >= 80 ? '#34c759' : '#007AFF'}; padding:2px 8px; border-radius:12px; font-weight:bold;">${displayScore}</span></td>
                <td style="font-family:'Roboto Mono';">${obi.toFixed(2)}</td>
                <td style="font-family:'Roboto Mono';">${vpin.toFixed(2)} (${riskText})</td>
                <td style="font-weight:600;">${item.status}</td>
            </tr>
        `;
        els.tbody.insertAdjacentHTML('beforeend', html);
    });
}

// 좌측 스캐너 리스트 (단순화된 정보)
function renderScannerList(targets) {
    if (!els.scannerList) return;
    els.scannerList.innerHTML = '';
    targets.forEach(item => {
        const displayScore = item.ai_prob <= 1 ? Math.round(item.ai_prob * 100) : parseFloat(item.ai_prob).toFixed(0);
        const html = `
            <div class="ticker-row" onclick="loadChartForTicker('${item.ticker}')">
                <div>
                    <div class="t-symbol">${item.ticker}</div>
                    <div class="t-name" style="font-size:10px; color:#8E8E93;">Score ${displayScore}</div>
                </div>
                <div class="t-price">$${item.price}</div>
            </div>`;
        els.scannerList.insertAdjacentHTML('beforeend', html);
    });
}

// 우측 신호 피드
function renderSignals(logs) {
    if (!els.signals) return;
    els.signals.innerHTML = '';
    logs.forEach(log => {
        const html = `
            <div class="signal-item" style="padding:12px; border-bottom:1px solid rgba(0,0,0,0.05);">
                <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                    <span class="signal-tag" style="background:#34c759; color:white; padding:2px 6px; border-radius:4px; font-size:9px; font-weight:bold;">BUY</span>
                    <span class="signal-time" style="font-size:10px; color:#8E8E93;">${log.timestamp}</span>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-weight:bold; color:#1D1D1F;">${log.ticker}</span>
                    <span style="font-family:'Roboto Mono'; font-weight:600;">$${log.price}</span>
                </div>
            </div>`;
        els.signals.insertAdjacentHTML('beforeend', html);
    });
}

/* ==========================================================================
   PART 3. CHART ENGINE (차트 기능)
   ========================================================================== */

async function loadChartForTicker(ticker) {
    if (!els.chartContainer) return;

    // [중요 수정] 기존 차트 인스턴스가 존재하면 메모리에서 해제 및 제거
    if (lightweightChart) {
        lightweightChart.remove();
        lightweightChart = null; 
        candleSeries = null;
    }
    
    // 차트 컨테이너 내부 HTML 비우기 (잔여 요소 제거)
    els.chartContainer.innerHTML = ''; 

    currentModalTicker = ticker;
    
    // 플레이스홀더 숨김 처리
    const placeholder = els.chartContainer.parentElement.querySelector('div[style*="absolute"]'); 
    if(placeholder) placeholder.style.display = 'none';

    // 새 차트 생성
    lightweightChart = createChart(els.chartContainer, {
        width: els.chartContainer.clientWidth, 
        height: 250, 
        layout: { backgroundColor: 'transparent', textColor: '#333' },
        grid: { vertLines: { color: 'rgba(0,0,0,0)' }, horzLines: { color: 'rgba(0,0,0,0.05)' } },
        timeScale: { timeVisible: true, secondsVisible: false, borderColor: 'rgba(0,0,0,0.1)' },
        rightPriceScale: { borderColor: 'rgba(0,0,0,0.1)' }
    });

    candleSeries = lightweightChart.addCandlestickSeries({
        upColor: '#34c759', downColor: '#ff3b30', borderVisible: false, wickUpColor: '#34c759', wickDownColor: '#ff3b30'
    });
    
    // 반응형 대응: 창 크기 조절 시 차트 크기 자동 조절 (선택 사항)
    // window.addEventListener('resize', () => {
    //     if(lightweightChart) lightweightChart.resize(els.chartContainer.clientWidth, 250);
    // });

    // 데이터 로드
    try {
        const res = await fetch(`/api/chart_data/${ticker}`);
        const json = await res.json();
        if(json.status === 'OK') {
            candleSeries.setData(json.results);
            lightweightChart.timeScale().fitContent();
        }
    } catch(e) { console.error("Chart Load Error:", e); }
}

// 3. 엔진 가동
setInterval(updateDashboard, 1500);
updateDashboard();

// DOM 로드 시 버튼 이벤트 연결 (여기에 알림 버튼 연결)
document.addEventListener('DOMContentLoaded', () => {
    
    // 알림 버튼
    const subscribeBtn = document.getElementById('subscribe-btn');
    if (subscribeBtn) subscribeBtn.addEventListener('click', requestNotificationPermission);

    // 설정 저장 버튼
    const saveScoreBtn = document.getElementById('save-score-btn');
    const minScoreInput = document.getElementById('min-score-input');
    if (saveScoreBtn && minScoreInput) {
        saveScoreBtn.addEventListener('click', async () => {
            if (!window.currentFCMToken) return alert("⚠️ 알림 권한이 없습니다. 'Alerts' 버튼을 먼저 눌러주세요.");
            try {
                await fetch('/api/set_alert_threshold', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: window.currentFCMToken, threshold: parseInt(minScoreInput.value) })
                });
                alert("✅ 설정 저장 완료!");
            } catch (e) { console.error(e); }
        });
    }

    // URL 파라미터 체크 (차트 자동 로딩)
    const urlParams = new URLSearchParams(window.location.search);
    const initialTicker = urlParams.get('ticker');
    if (initialTicker) {
        setTimeout(() => loadChartForTicker(initialTicker), 500);
    }
    
    // 서비스 워커
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(console.error);
    }
});