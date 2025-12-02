// static/js/sts.js
// (V6.0 - MacBook Silver Remaster: Logic Preserved, Visuals Updated)

import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

/* ==========================================================================
   PART 1. DATA ENGINE (기존 로직 100% 유지 + ID 매핑 수정)
   ========================================================================== */

// 맥북 스타일 HTML의 ID와 기존 로직을 연결하는 매핑 객체
const els = {
    // [수정됨] 현재 HTML에는 clock이 없으므로, 상단 상태바 텍스트 등을 활용하거나 생략
    // clock: document.getElementById('clock'), 
    
    // [중요] 중앙 테이블 (기존 target-table-body -> sts-target-table-body)
    tbody: document.getElementById('sts-target-table-body'),
    
    // [중요] 좌측 스캐너 리스트 (기존에는 없었으나 추가됨)
    scannerList: document.getElementById('ticker-list-container'),

    // [중요] 우측 신호 피드 (기존 session-signals -> signal-feed-container)
    signals: document.getElementById('signal-feed-container'),
    
    // 차트 컨테이너
    chartContainer: document.getElementById('chart-container')
};

// 전역 변수 (차트용)
let lightweightChart = null;
let candleSeries = null;
let currentModalTicker = null;

// 1. 데이터 가져오기 (Polling 방식: 1.5초마다 DB 조회 - 기존 유지)
async function updateDashboard() {
    try {
        const res = await fetch('/api/sts/status');
        if (!res.ok) throw new Error('Network Error');
        
        const data = await res.json();
        
        // 데이터가 없거나 비어있으면 리턴
        if (!data || !data.targets) return;

        // (A) Top Candidates Table 렌더링 (중앙 패널)
        renderTable(data.targets);
        
        // (B) Scanner List 렌더링 (좌측 패널 - 추가됨)
        renderScannerList(data.targets);
        
        // (C) Signals 렌더링 (우측 패널 - 추가됨)
        if (data.logs) renderSignals(data.logs);

        // (D) 상단 상태 텍스트 업데이트 (기존 로직 대체)
        const statusText = document.getElementById('scan-status-text');
        if (statusText) {
            statusText.innerText = data.targets.length > 0 ? "Active" : "Idle";
        }
        const countText = document.getElementById('scan-watching-count');
        if (countText) {
            countText.innerText = `${data.targets.length} Targets`;
        }

    } catch (e) {
        console.error("Dashboard Sync Error:", e);
    }
}

// 중앙 테이블 렌더링 (기존 로직 + 맥북 스타일 클래스 적용)
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
        // AI 점수가 1 이하(0.xx)면 100을 곱해서 표시, 아니면 그대로 표시
        const displayScore = scoreVal <= 1 ? Math.round(scoreVal * 100) : scoreVal.toFixed(0);
        const ticker = item.ticker;
        
        // 상태에 따른 스타일 (기존 로직 유지)
        let statusClass = 'text-tertiary'; // 기본
        if (item.status === 'FIRED') statusClass = 'text-down'; // 빨강
        else if (scoreVal >= 60 || item.status === 'AIMING') statusClass = 'text-up'; // 초록

        const obi = item.obi ? item.obi.toFixed(2) : "0.00";
        const vpin = item.vpin ? item.vpin.toFixed(2) : "0.00";

        // 맥북 스타일 테이블 행 생성
        const html = `
            <tr onclick="loadChartForTicker('${ticker}')" style="cursor:pointer;">
                <td style="font-weight:800; color:#1D1D1F;">${ticker}</td>
                <td style="font-family:'Roboto Mono'; font-weight:600;">$${price}</td>
                <td><span class="score-pill" style="background:${scoreVal >= 80 ? 'rgba(52, 199, 89, 0.15)' : 'rgba(0, 122, 255, 0.15)'}; color:${scoreVal >= 80 ? '#34c759' : '#007AFF'}; padding:2px 8px; border-radius:12px; font-weight:bold;">${displayScore}</span></td>
                <td style="font-family:'Roboto Mono';">${obi}</td>
                <td style="font-family:'Roboto Mono';">${vpin}</td>
                <td style="font-weight:600;" class="${statusClass}">${item.status}</td>
            </tr>
        `;
        els.tbody.insertAdjacentHTML('beforeend', html);
    });
}

// 좌측 스캐너 리스트 렌더링 (새로 추가된 UI 대응)
function renderScannerList(targets) {
    if (!els.scannerList) return;
    els.scannerList.innerHTML = '';

    targets.forEach(item => {
        const price = item.price ? parseFloat(item.price).toFixed(2) : "0.00";
        let rawScore = item.ai_score || item.ai_prob || 0;
        const displayScore = rawScore <= 1 ? Math.round(rawScore * 100) : rawScore.toFixed(0);

        const html = `
            <div class="ticker-row" onclick="loadChartForTicker('${item.ticker}')">
                <div>
                    <div class="t-symbol">${item.ticker}</div>
                    <div class="t-name" style="font-size:10px; color:#8E8E93;">Score ${displayScore}</div>
                </div>
                <div class="t-price">$${price}</div>
            </div>`;
        els.scannerList.insertAdjacentHTML('beforeend', html);
    });
}

// 우측 신호 피드 렌더링 (새로 추가된 UI 대응)
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
   PART 3. CHART ENGINE (차트 기능 추가)
   ========================================================================== */

async function loadChartForTicker(ticker) {
    currentModalTicker = ticker;
    
    if (!els.chartContainer) return;
    
    // 1. 차트 초기화 (없으면 생성)
    els.chartContainer.innerHTML = '';
    
    // 안내 문구 숨기기
    const placeholder = els.chartContainer.querySelector('div'); 
    if(placeholder) placeholder.style.display = 'none';

    lightweightChart = createChart(els.chartContainer, {
        width: els.chartContainer.clientWidth, 
        height: 250, // 높이 고정
        layout: { backgroundColor: 'transparent', textColor: '#333' },
        grid: { vertLines: { color: 'rgba(0,0,0,0)' }, horzLines: { color: 'rgba(0,0,0,0.05)' } },
        timeScale: { timeVisible: true, secondsVisible: false, borderColor: 'rgba(0,0,0,0.1)' },
        rightPriceScale: { borderColor: 'rgba(0,0,0,0.1)' }
    });

    candleSeries = lightweightChart.addCandlestickSeries({
        upColor: '#34c759', downColor: '#ff3b30', borderVisible: false, wickUpColor: '#34c759', wickDownColor: '#ff3b30'
    });

    // 2. 데이터 가져오기
    try {
        const res = await fetch(`/api/chart_data/${ticker}`);
        const json = await res.json();
        
        if(json.status === 'OK') {
            candleSeries.setData(json.results);
            lightweightChart.timeScale().fitContent();
        }
    } catch(e) { console.error("Chart Load Error:", e); }
}

// 3. 엔진 가동 (가장 중요한 부분)
// 1.5초마다 API를 때려서 화면을 갱신합니다. (기존 로직 유지)
setInterval(updateDashboard, 1500);
updateDashboard(); // 즉시 1회 실행

// URL 파라미터 체크 (대시보드에서 넘어온 경우 자동 차트 로딩)
document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    const initialTicker = urlParams.get('ticker');
    if (initialTicker) {
        setTimeout(() => loadChartForTicker(initialTicker), 500);
    }
});