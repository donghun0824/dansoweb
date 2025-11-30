/**
 * DANSO DASHBOARD APPLICATION (v25.0 - Premium Refactor)
 * ----------------------------------------------------
 * - Modular FCM Integration
 * - Lightweight Charts (Optimized)
 * - Robust Modal Lifecycle Management
 * - Premium UX Feedback & Error Handling
 */

// 1. Import Firebase Modules & Chart Library
import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

// 2. Firebase Configuration
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

// 3. Initialize Firebase Services
const app = initializeApp(firebaseConfig);
const messaging = getMessaging(app);

// Global Variables
window.currentFCMToken = null;
let lightweightChart = null;
let candleSeries = null;
let currentModalTicker = null;
let modalRefreshInterval = null;

// --- [Utility] Branded Logger ---
const log = (msg, data = '') => console.log(`%c[DANSO] ${msg}`, 'color: #00C853; font-weight: bold;', data);
const warn = (msg) => console.warn(`%c[DANSO WARN] ${msg}`, 'color: #FF9800; font-weight: bold;');
const errorLog = (msg, err) => console.error(`%c[DANSO ERROR] ${msg}`, 'color: #FF3B30; font-weight: bold;', err);


// --- [Core] FCM Notification Logic ---

function requestNotificationPermission() {
    log("Requesting notification permission...");

    if (!('Notification' in window)) {
        warn("Notifications not supported in this browser.");
        return;
    }
    
    // 1. Permission already granted
    if (Notification.permission === 'granted') {
        log("Permission already granted. Retrieving token.");
        getFCMToken();
        return;
    }

    // 2. Request Permission
    if (Notification.permission === 'default') {
        Notification.requestPermission().then((permission) => {
            if (permission === "granted") {
                log("Permission granted by user.");
                getFCMToken();
            } else {
                warn("Permission denied by user.");
            }
        });
    } else {
        // 3. Blocked
        warn("Permission permanently blocked. User must change browser settings.");
        alert("⚠️ Please enable notifications in your browser settings to receive signals.");
    }
}

function getFCMToken() {
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";

    navigator.serviceWorker.ready.then((activeRegistration) => {
        log("Service Worker active. Fetching token...");
        return getToken(messaging, { 
            vapidKey: VAPID_PUBLIC_KEY,
            serviceWorkerRegistration: activeRegistration 
        });
    }).then((currentToken) => {
        if (currentToken) {
            log("FCM Token acquired.");
            window.currentFCMToken = currentToken;
            sendTokenToServer(currentToken);
            
            // Visual Feedback
            const btn = document.getElementById('subscribe-btn');
            if(btn) {
                btn.innerHTML = '<i class="fas fa-check-circle"></i> Active';
                btn.style.borderColor = "var(--accent-green)";
                btn.style.color = "var(--text-primary)";
            }
        } else {
            warn("No registration token available.");
        }
    }).catch((err) => {
        errorLog("Token retrieval failed.", err);
    });
}

function sendTokenToServer(token) {
    fetch("/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token }),
    })
    .then(response => response.json())
    .then(data => log("Token registered with server."))
    .catch(err => errorLog("Failed to register token.", err));
}

// Foreground Message Handler
onMessage(messaging, (payload) => {
  log("Message received in foreground:", payload);
  new Notification(payload.notification.title, { 
      body: payload.notification.body,
      icon: "/static/images/danso_logo.png" 
  });
});


// --- [Main] Application Logic ---

document.addEventListener('DOMContentLoaded', function() {
    
    // --- [Setup] Notification Button ---
    const subscribeBtn = document.getElementById('subscribe-btn');
    if (subscribeBtn) {
        subscribeBtn.addEventListener('click', requestNotificationPermission);
    }

    // --- [Setup] Settings (Score Threshold) ---
    const saveScoreBtn = document.getElementById('save-score-btn');
    const minScoreInput = document.getElementById('min-score-input');

    if (saveScoreBtn) {
        saveScoreBtn.addEventListener('click', async () => {
            const score = minScoreInput.value;
            
            if (score === '' || score < 0 || score > 100) {
                alert("⚠️ Please enter a valid score between 0 and 100.");
                return;
            }

            const token = window.currentFCMToken;
            if (!token) {
                alert("⚠️ Notification permission is missing. Please click 'Enable Notifications' first.");
                return;
            }

            // UX Feedback: Loading State
            const originalText = saveScoreBtn.textContent;
            saveScoreBtn.textContent = "Saving...";
            saveScoreBtn.disabled = true;
            saveScoreBtn.style.opacity = "0.7";

            try {
                const response = await fetch('/api/set_alert_threshold', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: token, threshold: parseInt(score) }),
                });

                const result = await response.json();

                if (response.ok) {
                    alert(`✅ Saved! Alerts set for AI Score ${score}+.`);
                } else {
                    alert(`❌ Save failed: ${result.message}`);
                }
            } catch (error) {
                errorLog("Threshold save error", error);
                alert("Server error occurred. Please try again later.");
            } finally {
                saveScoreBtn.textContent = originalText;
                saveScoreBtn.disabled = false;
                saveScoreBtn.style.opacity = "1";
            }
        });
    }

    // --- [Setup] DOM Elements ---
    const scanStatusEl = document.getElementById('scan-status-text');
    const scanCountEl = document.getElementById('scan-watching-count');
    const tickerListContainer = document.getElementById('ticker-list-container');
    const signalFeedContainer = document.getElementById('signal-feed-container');
    const scrollingBarTrack = document.getElementById('scrolling-bar-track');
    
    // Modal Elements
    const modal = document.getElementById('ticker-modal');
    const modalTickerName = document.getElementById('modal-ticker-name');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    
    // Community Elements
    const postForm = document.getElementById('post-form'); 
    const postSubmitBtn = document.getElementById('post-submit-btn');
    const communityFeedContainer = document.getElementById('community-feed-container');


    // --- [Core] Data Fetching Logic (5s Interval) ---
    async function fetchDashboardData() {
        try {
            const response = await fetch('/api/dashboard');
            if (!response.ok) return;
            const data = await response.json();
            
            // 1. Update Scanner Status
            if (scanStatusEl && scanCountEl) {
                scanStatusEl.textContent = `Last Scan: ${data.status.last_scan_time || 'Waiting...'}`;
                scanCountEl.textContent = `Active Watchlist: ${data.status.watching_count || 0}`;
            }

            // 2. Render Watchlist (Ticker Chips)
            if (tickerListContainer) {
                tickerListContainer.innerHTML = '';
                if (data.status.watching_tickers && data.status.watching_tickers.length > 0) {
                    data.status.watching_tickers.forEach(item => {
                        const span = document.createElement('span');
                        span.textContent = item.ticker;
                        if (item.is_new) span.classList.add('new-ticker');
                        span.onclick = () => openTickerModal(item.ticker);
                        tickerListContainer.appendChild(span);
                    });
                } else {
                    tickerListContainer.innerHTML = '<p class="text-muted">Scanner initializing...</p>';
                }
            }
            
            // 3. Render Signal Feed (Cards)
            if (signalFeedContainer) {
                signalFeedContainer.innerHTML = '';
                let hasSignals = false;
                
                // Type A: Signals (Explosions)
                if (data.signals) {
                    data.signals.forEach(signal => {
                        hasSignals = true;
                        signalFeedContainer.innerHTML += createSignalCardHTML(signal, 'SIGNAL');
                    });
                }
                
                // Type B: Recommendations (Setups)
                if (data.recommendations) {
                    data.recommendations.forEach(rec => {
                        hasSignals = true;
                        signalFeedContainer.innerHTML += createSignalCardHTML(rec, 'SETUP');
                    });
                }
                
                if (!hasSignals) {
                    signalFeedContainer.innerHTML = '<div class="empty-state">Waiting for market activity...</div>';
                }
            }
        } catch (error) { 
            // Silent catch to prevent console spam on network blips
        }
    }

    // Helper: Create HTML for Signal Cards
    function createSignalCardHTML(data, type) {
        const isSignal = type === 'SIGNAL';
        const cardClass = isSignal ? 'signal-card signal' : 'signal-card recommendation';
        const label = isSignal ? 'EXPLOSION' : 'SETUP';
        const aiScore = data.probability_score !== undefined ? data.probability_score : 50;
        
        // Icons (SVG)
        const iconSignal = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="24" height="24"><path fill-rule="evenodd" d="M14.615 1.595a.75.75 0 01.359.852L12.982 9.75h7.268a.75.75 0 01.548 1.262l-10.5 11.25a.75.75 0 01-1.272-.71l1.992-7.302H3.75a.75.75 0 01-.548-1.262l10.5-11.25a.75.75 0 01.913-.143z" clip-rule="evenodd" /></svg>`;
        const iconSetup = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" width="24" height="24"><path d="M12 15a3 3 0 100-6 3 3 0 000 6z" /><path fill-rule="evenodd" d="M1.323 11.447C2.811 6.976 7.028 3.75 12.001 3.75c4.97 0 9.185 3.223 10.675 7.69.12.362.12.752 0 1.113-1.487 4.471-5.705 7.697-10.677 7.697-4.97 0-9.186-3.223-10.675-7.69a1.762 1.762 0 010-1.113zM17.25 12a5.25 5.25 0 11-10.5 0 5.25 5.25 0 0110.5 0z" clip-rule="evenodd" /></svg>`;

        return `
            <div class="${cardClass}" onclick="openTickerModal('${data.ticker}')">
                <div class="signal-icon-wrapper">
                    ${isSignal ? iconSignal : iconSetup}
                </div>
                <div class="info">
                    <div class="info-header">
                        <div class="ticker">${data.ticker}</div>
                        <span class="option-label">${label}</span>
                    </div>
                    <div class="signal-details">
                        <div class="price">@ $${parseFloat(data.price).toFixed(2)}</div>
                        ${ !isSignal ? `
                        <div class="ai-score">
                            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" width="14" height="14">
                              <path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 9.75h8.25L9.75 21l2.25-7.302H3.75z" />
                            </svg>
                            <span>${aiScore}%</span>
                        </div>` : '' }
                    </div>
                </div>
                <div class="time">${data.time.split(' ')[1]}</div>
            </div>`;
    }
    
    // --- [Market] Ticker Scroll Bar (One-time fetch) ---
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
                if (change > 0) {
                    changeClass = 'change-positive';
                    sign = '+';
                } else if (change < 0) {
                    changeClass = 'change-negative';
                }
                
                let valueStr = (typeof value === 'number') ? value.toFixed(2) : (value || '-');
                let changeStr = (typeof change === 'number') ? `${sign}${change.toFixed(2)}%` : '0.00%';

                return `
                    <div class="scroll-item">
                        <span class="name">${name}</span>
                        <span class="value">$${valueStr}</span>
                        <span class="${changeClass}">${changeStr}</span>
                    </div>
                `;
            };

            [...data.gainers, ...data.losers].forEach(t => {
                contentHtml += createItemHtml(t.ticker, t.day.c, t.todaysChangePerc);
            });

            // Duplicate content for infinite scroll illusion
            scrollingBarTrack.innerHTML = contentHtml + contentHtml;

        } catch (error) { 
            if(scrollingBarTrack) scrollingBarTrack.innerHTML = '<div class="scroll-item">Market data initializing...</div>'; 
        }
    }
    
    // --- [Community] Posts Fetching ---
    async function fetchCommunityPosts() {
        try {
            const response = await fetch('/api/posts');
            if (!response.ok) return;
            const data = await response.json();
            if (data.status !== 'OK') return;
            
            if (communityFeedContainer) {
                communityFeedContainer.innerHTML = '';
                if (data.posts.length > 0) {
                    data.posts.forEach(post => {
                        communityFeedContainer.innerHTML += `
                            <div class="post-card">
                                <div class="post-card-header">
                                    <span class="author">${post.author}</span>
                                    <span class="time">${post.time}</span>
                                </div>
                                <p class="post-card-content">${post.content}</p>
                            </div>
                        `;
                    });
                } else {
                    communityFeedContainer.innerHTML = '<div class="empty-state">Start the discussion...</div>';
                }
            }
        } catch (error) { }
    }
    
    // --- [Modal] Logic ---
    
    window.openTickerModal = async function(ticker) {
        log(`Opening modal for ${ticker}`);
        currentModalTicker = ticker;
        
        if(modal) {
            modal.style.display = 'block';
            // Slight delay for animation if CSS supported
            requestAnimationFrame(() => modal.classList.add('active'));
        }
        
        if(modalTickerName) modalTickerName.textContent = ticker;
        
        // Reset tabs to default state
        showModalTab('info', null, true); 
        
        // Loading states (Skeleton UI concept)
        const loadingHTML = '<p class="text-muted" style="padding: 20px;">Loading...</p>';
        document.getElementById('tab-info').innerHTML = loadingHTML;
        document.getElementById('tab-quote').innerHTML = loadingHTML;
        document.getElementById('tab-financials').innerHTML = loadingHTML;
        document.getElementById('chart-container').innerHTML = '<p class="text-muted" style="padding: 24px;">Initializing chart...</p>';
        
        // Parallel Data Fetching
        loadQuoteData(ticker);
        loadCompanyData(ticker);
        loadChartData(ticker);
        
        startModalAutoRefresh(ticker);
    }

    // Modal Data Functions
    async function loadQuoteData(ticker) {
        try {
            const res = await fetch(`/api/quote/${ticker}`);
            const data = await res.json();
            if (data.status !== 'error') {
                document.getElementById('tab-quote').innerHTML = `
                    <div class="quote-title">Live Quote</div>
                    <div class="quote-line"><span>Bid</span><span>$${data.bid_price} (x${data.bid_size})</span></div>
                    <div class="quote-line"><span>Ask</span><span>$${data.ask_price} (x${data.ask_size})</span></div>`;
            }
        } catch(e) { /* Ignore silently */ }
    }

    async function loadCompanyData(ticker) {
        try {
            const res = await fetch(`/api/details/${ticker}`);
            const data = await res.json();
            
            if (data.status === 'OK') {
                const d = data.results;
                const f = d.financials;
                
                document.getElementById('tab-info').innerHTML = `
                    <div class="company-details">
                        <img src="${d.logo_url}" alt="logo" onerror="this.style.display='none'"> 
                        <div class="info">
                            <h3>${d.name}</h3>
                            <div class="industry">${d.industry || 'Unknown Sector'}</div>
                        </div>
                    </div>
                    <div class="company-description">${d.description}</div>`;
                
                const fmt = (n) => (typeof n === 'number') ? n.toLocaleString() : '-';
                document.getElementById('tab-financials').innerHTML = `
                    <div class="quote-title">Financials</div>
                    <div class="quote-line"><span>Market Cap</span><span>$${fmt(f.market_cap)}</span></div>
                    <div class="quote-line"><span>P/E Ratio</span><span>${fmt(f.pe_ratio)}</span></div>
                    <div class="quote-line"><span>P/S Ratio</span><span>${fmt(f.ps_ratio)}</span></div>`;
            }
        } catch(e) {
            document.getElementById('tab-info').innerHTML = '<p class="text-muted">Details unavailable.</p>';
        }
    }

    async function loadChartData(ticker) {
        try {
            const res = await fetch(`/api/chart_data/${ticker}`);
            const data = await res.json();
            renderChart(data);
        } catch(e) {
            document.getElementById('chart-container').innerHTML = '<p class="text-muted">Chart unavailable.</p>';
        }
    }
    
    // --- [Modal] Close Logic ---
    function closeModal() {
        if(modal) {
            modal.style.display = 'none';
            modal.classList.remove('active');
        }
        stopModalAutoRefresh();
        
        if (lightweightChart) {
            lightweightChart.remove();
            lightweightChart = null;
        }
        candleSeries = null;
        currentModalTicker = null;
        document.getElementById('chart-container').innerHTML = ''; 
    }

    if(modalCloseBtn) modalCloseBtn.onclick = closeModal;
    window.onclick = function(event) {
        if (event.target == modal) closeModal();
    }

    // --- [Modal] Tab Switching ---
    window.showModalTab = function(tabName, clickedButton, isDefault = false) {
        document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
        document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
        
        const targetTab = document.getElementById(`tab-${tabName}`);
        if(targetTab) targetTab.classList.add('active');
        
        if(isDefault) {
            const defaultBtn = document.querySelector('.tab-button[onclick*="\'info\'"]');
            if(defaultBtn) defaultBtn.classList.add('active');
        } else if (clickedButton) {
            clickedButton.classList.add('active');
        }
    }
    
    // --- [Chart] Rendering (Visuals matched to CSS) ---
    function renderChart(chartData) {
        const container = document.getElementById('chart-container');
        if(!container) return;
        container.innerHTML = ''; 

        if (lightweightChart) {
            lightweightChart.remove();
            lightweightChart = null;
        }

        if (chartData.status !== 'OK' || !chartData.results || chartData.results.length === 0) {
            container.innerHTML = '<p class="text-muted" style="padding:24px;">No chart data available.</p>';
            return;
        }

        // Apply theme colors from CSS variables if possible, else hardcode premium theme
        const chartBg = 'transparent'; 
        const textColor = '#1D1D1F';
        const gridColor = 'rgba(0, 0, 0, 0.05)';
        const upColor = '#34C759'; // Apple Green
        const downColor = '#FF3B30'; // Apple Red

        lightweightChart = createChart(container, {
            width: container.clientWidth, 
            height: 350,
            layout: { 
                backgroundColor: chartBg, 
                textColor: textColor,
                fontFamily: "'Inter', sans-serif"
            },
            grid: { 
                vertLines: { color: gridColor },
                horzLines: { color: gridColor }
            },
            timeScale: { 
                timeVisible: true, 
                secondsVisible: false,
                borderColor: gridColor
            },
            rightPriceScale: {
                borderColor: gridColor
            }
        });

        candleSeries = lightweightChart.addCandlestickSeries({
            upColor: upColor,
            downColor: downColor,
            borderVisible: false,
            wickUpColor: upColor,
            wickDownColor: downColor
        });

        candleSeries.setData(chartData.results);
        lightweightChart.timeScale().fitContent();
    }

    // --- [Modal] Auto Refresh (5s) ---
    function startModalAutoRefresh(ticker) {
        stopModalAutoRefresh(); 
        
        modalRefreshInterval = setInterval(async () => {
            if (!modal || modal.style.display === 'none' || currentModalTicker !== ticker) {
                stopModalAutoRefresh();
                return;
            }
            // Silent refresh: Quote & Chart only
            loadQuoteData(ticker);
            
            try {
                const res = await fetch(`/api/chart_data/${ticker}`);
                const data = await res.json();
                if (data.status === 'OK' && candleSeries) {
                    candleSeries.setData(data.results);
                }
            } catch (e) {}
        }, 5000);
    }
    
    function stopModalAutoRefresh() {
        if (modalRefreshInterval) {
            clearInterval(modalRefreshInterval);
            modalRefreshInterval = null;
        }
    }
    
    // --- [Community] Post Submission ---
    if(postSubmitBtn) {
        postSubmitBtn.addEventListener('click', async function() {
            const author = postForm.elements['author'].value;
            const content = postForm.elements['content'].value;
            
            if (!content || !author) {
                alert('Please enter your name and a message.');
                return;
            }
            
            try {
                // UI Feedback
                postSubmitBtn.disabled = true;
                postSubmitBtn.textContent = 'Posting...';
                
                const response = await fetch('/api/posts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ author: author, content: content }),
                });
                
                const result = await response.json();
                
                if (result.status === 'OK') {
                    postForm.elements['content'].value = '';
                    fetchCommunityPosts(); // Refresh feed immediately
                } else {
                    alert(`Error: ${result.message}`);
                }
            } catch (error) {
                alert("Failed to post message.");
            } finally {
                postSubmitBtn.disabled = false;
                postSubmitBtn.textContent = 'Post Message';
            }
        });
    }

    // --- [Init] Start Application Loops ---
    // Initial fetch
    fetchMarketOverviewData(); 
    fetchDashboardData();
    fetchCommunityPosts();

    // Periodic intervals (5s)
    setInterval(() => {
        fetchDashboardData();   
        fetchCommunityPosts();  
    }, 5000);

    // --- [Init] PWA Service Worker ---
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js')
          .then(reg => log('ServiceWorker registered.', reg.scope))
          .catch(err => errorLog('ServiceWorker failed.', err));
      });
    }

}); // End DOMContentLoaded


// --- [Visual] Dashboard Background Animation (Metallic Vortex) ---
function initDashboardBg() {
    const canvas = document.getElementById('heroCanvas');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let width, height;
    
    function resize() {
        width = window.innerWidth;
        height = window.innerHeight;
        canvas.width = width;
        canvas.height = height;
    }
    window.addEventListener('resize', resize);
    resize();

    // Particles: Subtle Metallic Dust
    class Dust {
        constructor() { this.reset(); }
        reset() {
            this.x = Math.random() * width;
            this.y = Math.random() * height;
            this.vx = (Math.random() - 0.5) * 0.5;
            this.vy = (Math.random() - 0.5) * 0.5;
            this.size = Math.random() * 1.5;
            this.opacity = Math.random() * 0.3 + 0.1;
        }
        update() {
            this.x += this.vx;
            this.y += this.vy;
            if(this.x < 0 || this.x > width || this.y < 0 || this.y > height) this.reset();
            this.draw();
        }
        draw() {
            ctx.fillStyle = `rgba(100, 100, 110, ${this.opacity})`; // Metallic Grey
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
        }
    }

    const particles = [];
    for(let i=0; i<300; i++) particles.push(new Dust());

    function animate() {
        ctx.clearRect(0, 0, width, height);
        particles.forEach(p => p.update());
        requestAnimationFrame(animate);
    }
    animate();
}

document.addEventListener('DOMContentLoaded', initDashboardBg);