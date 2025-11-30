/**
 * DANSO DASHBOARD APPLICATION (v26.0 - Unified Endpoint + WebSocket)
 * ----------------------------------------------------
 * - Unified Data Fetching (/api/dashboard/combined)
 * - WebSocket Real-time Updates (/ws/dashboard)
 * - Redis-Cached Fallback Polling
 */

// 1. Import Modules
import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';
import { io } from "https://cdn.socket.io/4.7.5/socket.io.esm.min.js";

// 2. Firebase Config
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

// Global Variables
window.currentFCMToken = null;
let lightweightChart = null;
let candleSeries = null;
let currentModalTicker = null;
let modalRefreshInterval = null;
let socket = null;

// --- [Utility] Logger ---
const log = (msg, data = '') => console.log(`%c[DANSO] ${msg}`, 'color: #00C853; font-weight: bold;', data);
const warn = (msg) => console.warn(`%c[DANSO WARN] ${msg}`, 'color: #FF9800; font-weight: bold;');
const errorLog = (msg, err) => console.error(`%c[DANSO ERROR] ${msg}`, 'color: #FF3B30; font-weight: bold;', err);


// --- [Core] FCM Logic ---

function requestNotificationPermission() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') {
        getFCMToken();
    } else if (Notification.permission === 'default') {
        Notification.requestPermission().then(permission => {
            if (permission === "granted") getFCMToken();
        });
    } else {
        alert("⚠️ Please enable notifications in your browser settings.");
    }
}

function getFCMToken() {
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
    navigator.serviceWorker.ready.then((reg) => {
        return getToken(messaging, { vapidKey: VAPID_PUBLIC_KEY, serviceWorkerRegistration: reg });
    }).then((token) => {
        if (token) {
            window.currentFCMToken = token;
            sendTokenToServer(token);
            const btn = document.getElementById('subscribe-btn');
            if(btn) {
                btn.innerHTML = '<i class="fas fa-check-circle"></i> Active';
                btn.style.color = "var(--accent-green)";
            }
        }
    }).catch(err => errorLog("Token error", err));
}

function sendTokenToServer(token) {
    fetch("/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token }),
    }).catch(err => errorLog("Token register error", err));
}

onMessage(messaging, (payload) => {
  new Notification(payload.notification.title, { 
      body: payload.notification.body,
      icon: "/static/images/danso_logo.png" 
  });
});


// --- [Main] Application Logic ---

document.addEventListener('DOMContentLoaded', function() {

    // 1. DOM Elements
    const scanStatusEl = document.getElementById('scan-status-text');
    const scanCountEl = document.getElementById('scan-watching-count');
    const tickerListContainer = document.getElementById('ticker-list-container');
    const signalFeedContainer = document.getElementById('signal-feed-container');
    const scrollingBarTrack = document.getElementById('scrolling-bar-track');
    
    // STS Elements
    const stsTbody = document.getElementById('target-table-body');
    const microPanel = document.getElementById('micro-panel');
    const riskEntry = document.getElementById('risk-entry');
    const riskTarget = document.getElementById('risk-target');
    const riskStop = document.getElementById('risk-stop');
    const riskProb = document.getElementById('risk-prob');
    const sessionSignals = document.getElementById('session-signals');

    // Modal & Community
    const modal = document.getElementById('ticker-modal');
    const modalTickerName = document.getElementById('modal-ticker-name');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    const postSubmitBtn = document.getElementById('post-submit-btn');
    const postForm = document.getElementById('post-form');
    const communityFeedContainer = document.getElementById('community-feed-container');
    const saveScoreBtn = document.getElementById('save-score-btn');
    const minScoreInput = document.getElementById('min-score-input');

    // Event Listeners
    const subscribeBtn = document.getElementById('subscribe-btn');
    if (subscribeBtn) subscribeBtn.addEventListener('click', requestNotificationPermission);

    if (saveScoreBtn) {
        saveScoreBtn.addEventListener('click', async () => {
            const score = minScoreInput.value;
            if (score === '' || score < 0 || score > 100) {
                alert("Enter 0-100."); return;
            }
            if (!window.currentFCMToken) {
                alert("Enable notifications first."); return;
            }
            try {
                await fetch('/api/set_alert_threshold', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: window.currentFCMToken, threshold: parseInt(score) }),
                });
                alert("✅ Saved!");
            } catch (e) { alert("Error saving."); }
        });
    }


    // --- [Core] WebSocket Connection ---
    function initWebSocket() {
        socket = io('/ws/dashboard', { transports: ['websocket', 'polling'] });

        socket.on('connect', () => {
            log('WebSocket Connected');
        });

        socket.on('dashboard_update', (data) => {
            // log('WS Update received');
            updateUI(data);
        });

        socket.on('disconnect', () => {
            warn('WebSocket Disconnected');
        });
    }


    // --- [Core] Data Fetching (Fallback) ---
    async function fetchUnifiedData() {
        try {
            const response = await fetch('/api/dashboard/unified');
            if (!response.ok) return;
            const data = await response.json();
            updateUI(data);
        } catch (error) {
            // Silent error
        }
    }

    // --- [UI] Update Logic (Shared by WS & Fetch) ---
    function updateUI(data) {
        if (!data || !data.scanner || !data.sts) return;

        const scanner = data.scanner;
        const sts = data.sts;

        // 1. Update Scanner Status
        if (scanStatusEl) scanStatusEl.textContent = `Scan: ${scanner.status.last_scan_time || '...'}`;
        if (scanCountEl) scanCountEl.textContent = `Watch: ${scanner.status.watching_count || 0}`;

        // 2. Render Watchlist
        if (tickerListContainer) {
            tickerListContainer.innerHTML = '';
            if (scanner.status.watching_tickers && scanner.status.watching_tickers.length > 0) {
                scanner.status.watching_tickers.forEach(item => {
                    const span = document.createElement('span');
                    span.textContent = item.ticker;
                    if (item.is_new) span.classList.add('new-ticker');
                    span.onclick = () => openTickerModal(item.ticker);
                    tickerListContainer.appendChild(span);
                });
            } else {
                tickerListContainer.innerHTML = '<p class="text-muted">Scanning...</p>';
            }
        }

        // 3. Render Signals
        if (signalFeedContainer) {
            signalFeedContainer.innerHTML = '';
            let hasSignals = false;
            
            scanner.signals.forEach(sig => {
                hasSignals = true;
                signalFeedContainer.innerHTML += createSignalCardHTML(sig, 'SIGNAL');
            });
            scanner.recommendations.forEach(rec => {
                hasSignals = true;
                signalFeedContainer.innerHTML += createSignalCardHTML(rec, 'SETUP');
            });
            
            if (!hasSignals) signalFeedContainer.innerHTML = '<div class="empty-state">Waiting...</div>';
        }

        // 4. Render STS Sniper Table
        if (stsTbody) {
            stsTbody.innerHTML = '';
            if (sts.targets && sts.targets.length > 0) {
                sts.targets.forEach(t => {
                    const scoreVal = t.ai_score_raw || (t.ai_prob * 100) || 0;
                    const score = parseFloat(scoreVal).toFixed(1);
                    
                    let statusClass = 'watching';
                    let rowStyle = '';
                    if (t.status === 'FIRED') {
                        statusClass = 'aiming'; 
                        rowStyle = 'style="background: rgba(6, 193, 103, 0.05);"';
                    } else if (scoreVal >= 60 || t.status === 'AIMING') {
                        statusClass = 'aiming';
                    }

                    const obi = t.obi || 0;
                    const barWidth = Math.min(Math.abs(obi) * 50 + 50, 100);
                    
                    const html = `
                        <tr ${rowStyle}>
                            <td><span class="ticker-badge">$${t.ticker}</span></td>
                            <td class="mono-text">$${t.price.toFixed(2)}</td>
                            <td class="mono-text ${scoreVal >= 80 ? 'text-green' : 'text-dim'}">${score}</td>
                            <td><div class="mini-bar"><div style="width:${barWidth}%;"></div></div></td>
                            <td><span class="mono-text">VWAP ${t.vwap_dist ? t.vwap_dist.toFixed(2) : '0.0'}%</span></td>
                            <td><span class="status-badge ${statusClass}">${t.status}</span></td>
                        </tr>`;
                    stsTbody.insertAdjacentHTML('beforeend', html);
                });

                // Update Micro Panel with Top Target
                renderMicroPanel(sts.targets[0]);
                
                // Update Session Stats
                if(sessionSignals) sessionSignals.textContent = scanner.signals.length;
            } else {
                stsTbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:20px; color:#888;">Waiting for targets...</td></tr>';
            }
        }
    }

    function renderMicroPanel(topItem) {
        if (!microPanel || !topItem) return;
        
        const obi = (topItem.obi || 0).toFixed(2);
        const vpin = (topItem.vpin || 0).toFixed(2);
        const speed = topItem.tick_speed || 0;

        microPanel.innerHTML = `
            <div class="micro-row">
                <span>TARGET FOCUS</span>
                <span class="text-highlight mono-text">$${topItem.ticker}</span>
            </div>
            <div class="micro-item">
                <span>OBI (Order Flow)</span>
                <div class="progress-wrapper">
                    <span class="val-text mono-text ${obi > 0 ? 'text-green' : 'text-warn'}">${obi}</span>
                    <div class="progress-bg"><div class="progress-fill ${obi > 0 ? 'green' : 'warn'}" style="width: ${Math.min(Math.abs(obi)*100, 100)}%;"></div></div>
                </div>
            </div>
            <div class="micro-item">
                <span>VPIN (Toxicity)</span>
                <div class="progress-wrapper">
                    <span class="val-text mono-text text-dim">${vpin}</span>
                    <div class="progress-bg"><div class="progress-fill warn" style="width: ${vpin * 100}%;"></div></div>
                </div>
            </div>
            <div class="micro-item" style="border:none; padding-top:8px;">
                <span>Tape Speed</span>
                <span class="mono-text text-highlight">⚡ ${speed} ticks/s</span>
            </div>
        `;

        // Update Risk Engine
        const price = parseFloat(topItem.price || 0);
        const atrSim = price * 0.01; 
        
        if(riskEntry) riskEntry.innerText = '$' + price.toFixed(2);
        if(riskTarget) {
            riskTarget.innerText = "TRAILING 🚀"; 
            riskTarget.style.color = "#06C167";
        }
        if(riskStop) riskStop.innerText = '$' + (price - atrSim).toFixed(2);
        
        if(riskProb) {
            const prob = topItem.ai_score_raw || 0;
            riskProb.innerText = prob.toFixed(1) + '%';
            riskProb.className = prob >= 80 ? "value mono-text text-green" : "value mono-text";
        }
    }

    // Helper: Signal Cards HTML
    function createSignalCardHTML(data, type) {
        const isSignal = type === 'SIGNAL';
        const cardClass = isSignal ? 'signal-card signal' : 'signal-card recommendation';
        const label = isSignal ? 'EXPLOSION' : 'SETUP';
        const aiScore = data.probability_score !== undefined ? data.probability_score : 50;
        
        const iconSignal = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path fill-rule="evenodd" d="M14.615 1.595a.75.75 0 01.359.852L12.982 9.75h7.268a.75.75 0 01.548 1.262l-10.5 11.25a.75.75 0 01-1.272-.71l1.992-7.302H3.75a.75.75 0 01-.548-1.262l10.5-11.25a.75.75 0 01.913-.143z" clip-rule="evenodd" /></svg>`;
        const iconSetup = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="20" height="20"><path d="M12 15a3 3 0 100-6 3 3 0 000 6z" /><path fill-rule="evenodd" d="M1.323 11.447C2.811 6.976 7.028 3.75 12.001 3.75c4.97 0 9.185 3.223 10.675 7.69.12.362.12.752 0 1.113-1.487 4.471-5.705 7.697-10.677 7.697-4.97 0-9.186-3.223-10.675-7.69a1.762 1.762 0 010-1.113zM17.25 12a5.25 5.25 0 11-10.5 0 5.25 5.25 0 0110.5 0z" clip-rule="evenodd" /></svg>`;

        return `
            <div class="${cardClass}" onclick="openTickerModal('${data.ticker}')">
                <div class="signal-icon-wrapper">${isSignal ? iconSignal : iconSetup}</div>
                <div class="info">
                    <div class="info-header">
                        <div class="ticker">${data.ticker}</div>
                        <span class="option-label">${label}</span>
                    </div>
                    <div class="signal-details">
                        <div class="price">@ $${parseFloat(data.price).toFixed(2)}</div>
                        ${ !isSignal ? `<div class="ai-score"><span>${aiScore}%</span></div>` : '' }
                    </div>
                </div>
                <div class="time">${data.time.split(' ')[0]}</div>
            </div>`;
    }

    // --- [Ticker Scroll] ---
    async function fetchMarketOverviewData() {
        try {
            const response = await fetch('/api/market_overview');
            if (!response.ok) return;
            const data = await response.json();
            if (data.status !== 'OK' || !scrollingBarTrack) return;

            let contentHtml = ''; 
            const createItemHtml = (name, value, change) => {
                let changeClass = 'change-zero';
                let sign = '';
                if (change > 0) { changeClass = 'change-positive'; sign = '+'; }
                else if (change < 0) { changeClass = 'change-negative'; }
                return `<div class="scroll-item"><span class="name">${name}</span><span class="value">$${value.toFixed(2)}</span><span class="${changeClass}">${sign}${change.toFixed(2)}%</span></div>`;
            };

            [...data.gainers, ...data.losers].forEach(t => {
                contentHtml += createItemHtml(t.ticker, t.day.c, t.todaysChangePerc);
            });
            scrollingBarTrack.innerHTML = contentHtml + contentHtml;
        } catch (e) {}
    }

    // --- [Community] ---
    async function fetchCommunityPosts() {
        try {
            const response = await fetch('/api/posts');
            if (!response.ok) return;
            const data = await response.json();
            if (communityFeedContainer) {
                communityFeedContainer.innerHTML = '';
                if (data.posts.length > 0) {
                    data.posts.forEach(post => {
                        communityFeedContainer.innerHTML += `<div class="post-card"><div class="post-card-header"><span class="author">${post.author}</span><span class="time">${post.time}</span></div><p class="post-card-content">${post.content}</p></div>`;
                    });
                } else {
                    communityFeedContainer.innerHTML = '<div class="empty-state">No posts yet.</div>';
                }
            }
        } catch (e) {}
    }

    if(postSubmitBtn) {
        postSubmitBtn.addEventListener('click', async function() {
            const author = postForm.elements['author'].value;
            const content = postForm.elements['content'].value;
            if (!content || !author) { alert('Required fields missing.'); return; }
            
            postSubmitBtn.disabled = true;
            await fetch('/api/posts', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ author, content }),
            });
            postForm.elements['content'].value = '';
            fetchCommunityPosts();
            postSubmitBtn.disabled = false;
        });
    }

    // --- [Modal] ---
    window.openTickerModal = async function(ticker) {
        currentModalTicker = ticker;
        if(modal) { modal.style.display = 'block'; modal.classList.add('active'); }
        if(modalTickerName) modalTickerName.textContent = ticker;
        showModalTab('info', null, true); 

        // Parallel Load
        loadQuoteData(ticker);
        loadCompanyData(ticker);
        loadChartData(ticker);
        startModalAutoRefresh(ticker);
    }

    async function loadQuoteData(ticker) {
        try {
            const res = await fetch(`/api/quote/${ticker}`);
            const data = await res.json();
            if (data.status !== 'error') {
                document.getElementById('tab-quote').innerHTML = `<div class="quote-title">Live Quote</div><div class="quote-line"><span>Bid</span><span>$${data.bid_price} (x${data.bid_size})</span></div><div class="quote-line"><span>Ask</span><span>$${data.ask_price} (x${data.ask_size})</span></div>`;
            }
        } catch(e) {}
    }

    async function loadCompanyData(ticker) {
        try {
            const res = await fetch(`/api/details/${ticker}`);
            const data = await res.json();
            if (data.status === 'OK') {
                const d = data.results;
                const f = d.financials;
                document.getElementById('tab-info').innerHTML = `<div class="company-details"><img src="${d.logo_url}" onerror="this.style.display='none'"> <div class="info"><h3>${d.name}</h3><div class="industry">${d.industry}</div></div></div><div class="company-description">${d.description}</div>`;
                document.getElementById('tab-financials').innerHTML = `<div class="quote-title">Financials</div><div class="quote-line"><span>Market Cap</span><span>$${f.market_cap.toLocaleString()}</span></div><div class="quote-line"><span>P/E</span><span>${f.pe_ratio}</span></div>`;
            }
        } catch(e) {}
    }

    async function loadChartData(ticker) {
        try {
            const res = await fetch(`/api/chart_data/${ticker}`);
            const data = await res.json();
            renderChart(data);
        } catch(e) {}
    }

    function renderChart(chartData) {
        const container = document.getElementById('chart-container');
        if(!container) return;
        container.innerHTML = ''; 
        if (lightweightChart) { lightweightChart.remove(); lightweightChart = null; }

        if (chartData.status !== 'OK' || !chartData.results || chartData.results.length === 0) {
            container.innerHTML = '<p class="text-muted" style="padding:24px;">No Data</p>';
            return;
        }

        lightweightChart = createChart(container, {
            width: container.clientWidth, height: 350,
            layout: { backgroundColor: 'transparent', textColor: '#1D1D1F', fontFamily: "'Inter', sans-serif" },
            grid: { vertLines: { color: 'rgba(0,0,0,0.05)' }, horzLines: { color: 'rgba(0,0,0,0.05)' } },
            timeScale: { timeVisible: true, secondsVisible: false }
        });
        candleSeries = lightweightChart.addCandlestickSeries({
            upColor: '#34C759', downColor: '#FF3B30', borderVisible: false, wickUpColor: '#34C759', wickDownColor: '#FF3B30'
        });
        candleSeries.setData(chartData.results);
        lightweightChart.timeScale().fitContent();
    }

    function closeModal() {
        if(modal) { modal.style.display = 'none'; modal.classList.remove('active'); }
        stopModalAutoRefresh();
        if (lightweightChart) { lightweightChart.remove(); lightweightChart = null; }
        candleSeries = null;
    }
    if(modalCloseBtn) modalCloseBtn.onclick = closeModal;
    window.onclick = (e) => { if(e.target == modal) closeModal(); };

    window.showModalTab = function(tabName, clickedButton, isDefault = false) {
        document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
        document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
        document.getElementById(`tab-${tabName}`).classList.add('active');
        if(isDefault) document.querySelector('.tab-button[onclick*="\'info\'"]').classList.add('active');
        else if (clickedButton) clickedButton.classList.add('active');
    }

    function startModalAutoRefresh(ticker) {
        stopModalAutoRefresh(); 
        modalRefreshInterval = setInterval(async () => {
            if (!modal || modal.style.display === 'none' || currentModalTicker !== ticker) return stopModalAutoRefresh();
            loadQuoteData(ticker);
            try {
                const res = await fetch(`/api/chart_data/${ticker}`);
                const data = await res.json();
                if (data.status === 'OK' && candleSeries) candleSeries.setData(data.results);
            } catch(e) {}
        }, 5000);
    }
    function stopModalAutoRefresh() {
        if (modalRefreshInterval) { clearInterval(modalRefreshInterval); modalRefreshInterval = null; }
    }

    // --- [Initialization] ---
    initWebSocket();
    fetchMarketOverviewData();
    fetchUnifiedData();
    fetchCommunityPosts();
    setInterval(fetchUnifiedData, 2000); // Polling fallback (2s)
    setInterval(fetchCommunityPosts, 5000);

    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js');
      });
    }
});