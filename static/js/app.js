// app.js (v9.0 - Production Optimized: Polling, Token, Chart Refresh)

import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

// 1. Config
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

// --- [1] 알림 (토큰 안정성 강화) ---
async function requestNotificationPermission() {
    if (!('Notification' in window)) return alert("알림 미지원");
    
    const permission = await Notification.requestPermission();
    if (permission === 'granted') {
        getFCMToken();
    } else {
        alert("알림 권한이 차단되었습니다.");
    }
}

async function getFCMToken() {
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
    
    try {
        // [최적화] 서비스워커 준비 확실히 대기
        const registration = await navigator.serviceWorker.ready;
        const token = await getToken(messaging, { 
            vapidKey: VAPID_PUBLIC_KEY, 
            serviceWorkerRegistration: registration 
        });

        if (token) {
            window.currentFCMToken = token;
            sendTokenToServer(token);
            alert("✅ 알림 활성화 완료!");
        }
    } catch (err) {
        console.error("Token Error:", err);
    }
}

function sendTokenToServer(token) {
    fetch("/subscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }).catch(console.error);
}

onMessage(messaging, (payload) => {
    new Notification(payload.notification.title, { body: payload.notification.body, icon: "/static/images/danso_logo.png" });
});


// --- [2] 메인 로직 ---
document.addEventListener('DOMContentLoaded', function() {

    // 버튼 연결
    const subscribeBtn = document.getElementById('subscribe-btn');
    if (subscribeBtn) subscribeBtn.addEventListener('click', requestNotificationPermission);

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

    // 모달 닫기
    const modal = document.getElementById('ticker-modal');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    if (modal && modalCloseBtn) {
        modalCloseBtn.onclick = closeModal;
        window.onclick = (e) => { if (e.target == modal) closeModal(); };
    }

    // --- [수정됨] 실시간 데이터 가져오기 (엔드포인트 복구) ---
    async function fetchDashboardData() {
        try {
            // 🚨 핵심 수정: '/api/dashboard' -> '/api/sts/status'로 변경
            // 이제 data.targets와 data.logs가 정상적으로 들어옵니다.
            const response = await fetch('/api/sts/status'); 
            const data = await response.json();
            
            // 2. 상태 텍스트 갱신
            const statusText = document.getElementById('scan-status-text');
            if (statusText && data.targets) {
                statusText.innerHTML = data.targets.length > 0 ? '<span style="color:#34c759">● Active</span>' : 'Idle';
                const countEl = document.getElementById('scan-watching-count');
                if(countEl) countEl.textContent = `${data.targets.length} Targets`;
            }

            // 3. 테이블/리스트 그리기
            const listContainer = document.getElementById('ticker-list-container');
            const stsTableBody = document.getElementById('sts-target-table-body');

            // (Type A) 대시보드 메인 테이블 (클릭 시 STS로 이동)
            if (listContainer && listContainer.tagName === 'TBODY') {
                listContainer.innerHTML = '';
                // data.targets가 이제 존재하므로 루프가 정상 작동합니다.
                data.targets.forEach(t => {
                    const scoreDisplay = t.ai_prob > 0 ? Math.round(t.ai_prob * 100) : '<i class="fa-solid fa-spinner fa-spin"></i>';
                    
                    const row = `
                        <tr onclick="window.location.href='/sts?ticker=${t.ticker}'" style="cursor:pointer;">
                            <td style="font-weight:800;">${t.ticker}</td>
                            <td>$${t.price}</td>
                            <td style="color:${t.ai_prob > 0.7 ? '#34c759' : '#1D1D1F'}">${scoreDisplay}</td>
                            <td>${t.obi.toFixed(2)}</td>
                            <td>${t.status}</td>
                        </tr>`;
                    listContainer.insertAdjacentHTML('beforeend', row);
                });
            } 
            // (Type B) STS 사이드바 리스트
            else if (listContainer && listContainer.tagName === 'DIV') {
                listContainer.innerHTML = '';
                data.targets.forEach(t => {
                    const row = `
                        <div class="ticker-row" onclick="openTickerModal('${t.ticker}')">
                            <div><div class="t-symbol">${t.ticker}</div><div class="t-name" style="font-size:10px; color:#8E8E93;">Score ${Math.round(t.ai_prob * 100)}</div></div>
                            <div class="t-price">$${t.price}</div>
                        </div>`;
                    listContainer.insertAdjacentHTML('beforeend', row);
                });
            }

            // (Type C) STS 메인 테이블
            if (stsTableBody) {
                stsTableBody.innerHTML = '';
                data.targets.forEach(t => {
                    const row = `
                        <tr onclick="openTickerModal('${t.ticker}')" style="cursor:pointer;">
                            <td style="font-weight:800;">${t.ticker}</td>
                            <td>$${t.price}</td>
                            <td><span class="score-badge">${Math.round(t.ai_prob * 100)}</span></td>
                            <td>${t.obi.toFixed(2)}</td>
                            <td>${t.vpin.toFixed(2)}</td>
                            <td>${t.status}</td>
                        </tr>`;
                    stsTableBody.insertAdjacentHTML('beforeend', row);
                });
            }

            // 4. 신호 피드 (Signals)
            const signalContainer = document.getElementById('signal-feed-container');
            if (signalContainer && data.logs) {
                signalContainer.innerHTML = '';
                data.logs.forEach(log => {
                    const item = `
                        <div class="signal-item" style="padding:10px; border-bottom:1px solid rgba(0,0,0,0.05);">
                            <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                <span class="signal-tag" style="background:#34c759; color:white; padding:2px 6px; border-radius:4px; font-size:10px;">BUY</span>
                                <span class="signal-time">${log.timestamp}</span>
                            </div>
                            <div style="display:flex; justify-content:space-between;"><span style="font-weight:bold;">${log.ticker}</span><span>$${log.price}</span></div>
                        </div>`;
                    signalContainer.insertAdjacentHTML('beforeend', item);
                });
            }
        } catch (e) { console.log("Polling error:", e); }
    }

    async function fetchCommunityPosts() {
        const container = document.getElementById('community-feed-container');
        if (!container) return;
        try {
            const res = await fetch('/api/posts');
            const data = await res.json();
            if (data.status === 'OK') {
                container.innerHTML = '';
                const myName = document.querySelector('input[name="author"]')?.value || 'Guest';
                data.posts.forEach(p => {
                    const isMe = p.author === myName;
                    container.insertAdjacentHTML('beforeend', `<div style="display:flex; flex-direction:column; align-items:${isMe?'flex-end':'flex-start'}; margin-bottom:8px;"><span style="font-size:10px; color:#aaa;">${p.author}</span><div style="background:${isMe?'#007AFF':'#E5E5EA'}; color:${isMe?'white':'black'}; padding:6px 12px; border-radius:12px; font-size:12px; max-width:85%;">${p.content}</div></div>`);
                });
                container.scrollTop = container.scrollHeight;
            }
        } catch(e){}
    }

    // --- [최적화] Polling 통합 및 주기 조절 ---
    // (기존 2초 -> 5초로 늘리고 Promise.all로 묶어서 요청 수 절감)
    async function updateAllData() {
        await Promise.all([
            fetchDashboardData(),
            fetchCommunityPosts()
        ]);
    }
    
    // 5초마다 갱신 (비용 최적화)
    setInterval(updateAllData, 5000);
    updateAllData(); // 초기 실행
    
    // 시장 데이터는 1번만
    fetchMarketOverview();

    // --- Chart Logic ---
    let lightweightChart = null;
    let candleSeries = null;
    let currentModalTicker = null;
    let chartRefreshInterval = null; // [부활] 차트 자동 갱신용 타이머

    window.openTickerModal = async function(ticker) {
        if (!document.getElementById('ticker-modal')) {
            window.location.href = `/sts?ticker=${ticker}`; return;
        }
        
        currentModalTicker = ticker;
        const m = document.getElementById('ticker-modal');
        if(m) {
            m.style.display = 'block';
            document.getElementById('modal-ticker-name').textContent = ticker;
            
            const chartContainer = document.getElementById('chart-container');
            if (chartContainer) {
                chartContainer.innerHTML = ''; 
                lightweightChart = createChart(chartContainer, {
                    width: chartContainer.clientWidth, height: 350,
                    layout: { backgroundColor: '#ffffff', textColor: '#333' },
                    grid: { vertLines: { color: '#f0f0f0' }, horzLines: { color: '#f0f0f0' } },
                    timeScale: { timeVisible: true, secondsVisible: false }
                });
                candleSeries = lightweightChart.addCandlestickSeries({
                    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false, wickUpColor: '#26a69a', wickDownColor: '#ef5350'
                });
                
                await updateChartData(ticker); // 최초 데이터 로드
                startChartAutoRefresh(ticker); // [부활] 자동 갱신 시작
            }
        }
    };

    // 차트 데이터 가져오는 함수 분리
    async function updateChartData(ticker) {
        try {
            const res = await fetch(`/api/chart_data/${ticker}`);
            const json = await res.json();
            if(json.status === 'OK' && candleSeries) {
                candleSeries.setData(json.results);
                lightweightChart.timeScale().fitContent();
            }
        } catch(e) { console.error("Chart Error:", e); }
    }

    // [부활] 차트 5초마다 갱신 (모달 열려있을 때만)
    function startChartAutoRefresh(ticker) {
        stopChartAutoRefresh(); // 기존 타이머 제거
        chartRefreshInterval = setInterval(() => {
            // 모달이 닫혀있거나 다른 티커로 바뀌면 중단
            const m = document.getElementById('ticker-modal');
            if (!m || m.style.display === 'none' || currentModalTicker !== ticker) {
                stopChartAutoRefresh();
                return;
            }
            updateChartData(ticker);
        }, 5000);
    }

    function stopChartAutoRefresh() {
        if (chartRefreshInterval) {
            clearInterval(chartRefreshInterval);
            chartRefreshInterval = null;
        }
    }

    function closeModal() {
        const m = document.getElementById('ticker-modal');
        if(m) m.style.display = 'none';
        stopChartAutoRefresh(); // 닫을 때 갱신 중지
        if(lightweightChart) { lightweightChart.remove(); lightweightChart = null; }
    }
});

// Helper Functions
async function fetchMarketOverview() {
    // ... (기존과 동일, 생략 없이 전체 코드에 포함됨) ...
    const gainersList = document.getElementById('top-gainers-list');
    const losersList = document.getElementById('top-losers-list');
    const scrollTrack = document.getElementById('scrolling-bar-track');
    
    try {
        const res = await fetch('/api/market_overview');
        const data = await res.json();
        if (data.status !== 'OK') return;

        if (scrollTrack) {
            let html = '';
            const allItems = [...data.gainers, ...data.losers];
            const makeItem = (t) => `<div style="margin-right:30px; display:inline-block;"><span style="font-weight:bold;">${t.ticker}</span> <span style="color:${t.todaysChangePerc>0?'#34c759':'#ff3b30'}">${t.todaysChangePerc.toFixed(2)}%</span></div>`;
            allItems.forEach(t => html += makeItem(t));
            allItems.forEach(t => html += makeItem(t)); 
            scrollTrack.innerHTML = html; 
            scrollTrack.style.animation = "scroll-left 30s linear infinite"; 
        }

        const renderList = (container, items, isGainer) => {
            if(!container) return;
            container.innerHTML = '';
            items.forEach(t => {
                const colorClass = isGainer ? 'text-up' : 'text-down';
                const sign = isGainer ? '+' : '';
                const html = `<div class="mini-list-item"><span class="mini-ticker">${t.ticker}</span><span class="mini-change ${colorClass}">${sign}${t.todaysChangePerc.toFixed(2)}%</span></div>`;
                container.insertAdjacentHTML('beforeend', html);
            });
        };
        renderList(gainersList, data.gainers, true);
        renderList(losersList, data.losers, false);
    } catch (e) { console.error(e); }
}

const style = document.createElement('style');
style.innerHTML = `@keyframes scroll-left { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }`;
document.head.appendChild(style);

if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(console.error); });
}