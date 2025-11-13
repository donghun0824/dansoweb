// app.js (v2 - 모듈형 SDK / 타이밍 오류 수정)

// 1. Firebase 모듈 가져오기 (CDN에서 바로 가져옴)
import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";

// 2. ✅ 사용자님의 firebaseConfig
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

// 3. Firebase 초기화
const app = initializeApp(firebaseConfig);
const messaging = getMessaging(app);

// 4. FCM 함수 정의
function requestNotificationPermission() {
    console.log("Requesting notification permission...");
    
    Notification.requestPermission().then((permission) => {
        if (permission === "granted") {
            console.log("Notification permission granted.");
            // 권한이 승인되면 토큰 가져오기 실행
            getFCMToken();
        } else {
            console.log("Notification permission denied.");
        }
    });
}

// ✅ [수정 1] 서비스워커가 'active' 될 때까지 기다리도록 수정
function getFCMToken() {
    // 5. ✅ 사용자님의 VAPID 공개 키
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";

    // ✅ 서비스워커가 'active' 될 때까지 기다립니다.
    navigator.serviceWorker.ready.then((activeRegistration) => {
        
        console.log("Service Worker is active, retrieving token...");

        // 서비스워커가 준비되면 getToken을 호출합니다.
        return getToken(messaging, { 
            vapidKey: VAPID_PUBLIC_KEY,
            serviceWorkerRegistration: activeRegistration // 'active' 워커를 명시
        });

    }).then((currentToken) => {
        if (currentToken) {
            console.log("FCM Token:", currentToken);
            // 6. ✅ 이 토큰을 우리 DB에 저장합니다.
            sendTokenToServer(currentToken);
        } else {
            console.log("No registration token available. Request permission to generate one.");
        }
    }).catch((err) => {
        console.log("An error occurred while retrieving token (sw.js active wait failed?). ", err);
    });
}


function sendTokenToServer(token) {
    // 7. 이 'token'을 Render의 PostgreSQL DB에 저장하는 API를 호출합니다.
    fetch("/subscribe", { // (API 주소는 예시입니다)
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({ token: token }),
    })
    .then(response => response.json())
    .then(data => {
        console.log("Token sent to server:", data);
    })
    .catch((error) => {
        console.error("Error sending token to server:", error);
    });
}

// 8. (선택사항) 앱이 "켜져 있을 때" (포그라운드) 알림 받기
onMessage(messaging, (payload) => {
  console.log("Message received in foreground: ", payload);
  new Notification(payload.notification.title, { 
      body: payload.notification.body,
      icon: "/static/images/danso_logo.png" 
  });
});


// --- (기존 app.js 코드 시작) ---
document.addEventListener('DOMContentLoaded', function() {
    
    // 9. ✅ [수정 2] 페이지 로드 시 바로 호출하던 코드를 주석 처리 (너무 빠름)
    // requestNotificationPermission();

    // --- 1. DOM 요소 가져오기 (v11.0) ---
    const scanStatusEl = document.getElementById('scan-status-text');
    const scanCountEl = document.getElementById('scan-watching-count');
    const tickerListContainer = document.getElementById('ticker-list-container');
    const signalFeedContainer = document.getElementById('signal-feed-container');
    
    // (v3.0) 스크롤링 바 요소
    const scrollingBarTrack = document.getElementById('scrolling-bar-track');
    
    const modal = document.getElementById('ticker-modal');
    const modalTickerName = document.getElementById('modal-ticker-name');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    
    const postForm = document.getElementById('post-form'); 
    const postSubmitBtn = document.getElementById('post-submit-btn');
    const communityFeedContainer = document.getElementById('community-feed-container');
    
    let modalRefreshInterval = null;
    
    let lightweightChart = null;
    let candleSeries = null;
    let currentModalTicker = null; 

    // --- 2. 5초마다 데이터 새로고침 (DB) ---
    async function fetchDashboardData() {
        try {
            const response = await fetch('/api/dashboard');
            if (!response.ok) return;
            const data = await response.json();
            
            // (v10.1) 상태 카드 업데이트 (영어)
            scanStatusEl.textContent = `Scanner Scan: ${data.status.last_scan_time}`;
            scanCountEl.textContent = `Watching: ${data.status.watching_count} tickers`;
            tickerListContainer.innerHTML = '';
            if (data.status.watching_tickers.length > 0) {
                data.status.watching_tickers.forEach(item => {
                    const span = document.createElement('span');
                    span.textContent = item.ticker;
                    if (item.is_new) span.classList.add('new-ticker');
                    span.addEventListener('click', () => openTickerModal(item.ticker));
                    tickerListContainer.appendChild(span);
                });
            } else {
                 // (v10.1) 영어
                tickerListContainer.innerHTML = '<p style="color: #8b95a1;">Loading scanner...</p>';
            }
            
            // ✅ (v11.0) "v3.1" CSS에 맞춘 신호 피드 HTML (텍스트가 <span> 안에 있음)
            signalFeedContainer.innerHTML = '';
            let hasSignals = false;
            
            // [엔진 1: 폭발] 신호 (signals)
            data.signals.forEach(signal => {
                hasSignals = true;
                signalFeedContainer.innerHTML += `
                    <div class="signal-card signal" onclick="openTickerModal('${signal.ticker}')">
                        <div class="signal-icon-wrapper">
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path fill-rule="evenodd" d="M11.484 2.17a.75.75 0 0 1 1.032 0L12 3.013l.484-.843a.75.75 0 0 1 1.032 0l4.312 7.5a.75.75 0 0 1-.515 1.077l-4.78 1.002a.75.75 0 0 0-.57 1.137l.635 1.517a.75.75 0 0 1-1.12.935l-3.334-4.001a.75.75 0 0 1-.002-.953l2.845-3.097a.75.75 0 0 0 .041-.85l-1.476-2.951Z" clip-rule="evenodd" /><path d="M12.481 12.355a.75.75 0 0 0-.57 1.137l.635 1.517a.75.75 0 0 1-1.12.935l-3.334-4.001a.75.75 0 0 1-.002-.953l2.845-3.097a.75.75 0 0 0 .041-.85l-1.476-2.951a.75.75 0 0 1-.515-1.077L.816 12.013a.75.75 0 0 0 .515 1.077l4.78-1.002a.75.75 0 0 1 .57-1.137l-.635-1.517a.75.75 0 0 0 1.12-.935l3.334 4.001a.75.75 0 0 0 .002.953l-2.845 3.097a.75.75 0 0 0-.041.85l1.476 2.951a.75.75 0 0 1 .515 1.077l5.093-8.825Z" /> </svg>
                        </div>
                        <div class="info">
                            <div class="info-header">
                                <div class="ticker">${signal.ticker}</div>
                                <span class="option-label">EXPLOSION</span>
                            </div>
                            <div class="signal-details">
                                <div class="price">@ $${signal.price}</div>
                            </div>
                        </div>
                        <div class="time">${signal.time}</div>
                    </div>`;
            });
            
            // [엔진 2: 셋업] 신호 (recommendations)
            data.recommendations.forEach(rec => {
                hasSignals = true;
                // (v10.0) AI 점수 (probability_score)를 DB에서 가져옴
                const aiScore = rec.probability_score !== null ? rec.probability_score : 50; // (기본값 50)
                
                signalFeedContainer.innerHTML += `
                    <div class="signal-card recommendation" onclick="openTickerModal('${rec.ticker}')">
                        <div class="signal-icon-wrapper">
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path fill-rule="evenodd" d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12Zm11.85-3.904a.75.75 0 0 0-1.08-.02L9 12.378l-1.95-1.95a.75.75 0 1 0-1.06 1.06l2.5 2.5a.75.75 0 0 0 1.06 0l4.5-4.5a.75.75 0 0 0 .02-1.08Z" clip-rule="evenodd" /></svg>
                        </div>
                        <div class="info">
                             <div class="info-header">
                                <div class="ticker">${rec.ticker}</div>
                                <span class="option-label">SETUP</span>
                            </div>
                            <div class="signal-details">
                                <div class="price">@ $${rec.price}</div>
                                <div class="ai-score">
                                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                                      <path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L21.75 5.25l-.813 2.846a4.5 4.5 0 0 0-3.09 3.09L15 12l2.846.813a4.5 4.5 0 0 0 3.09 3.09L21.75 18.75l.813-2.846a4.5 4.5 0 0 0-3.09-3.09L18.25 12Z" />
                                    </svg>
                                    <span>${aiScore}%</span>
                                </div>
                            </div>
                        </div>
                        <div class="time">${rec.time}</div>
                    </div>`;
            });
            
            if (!hasSignals) {
                signalFeedContainer.innerHTML = '<div class="empty-state">No active signals yet...</div>';
            }
        } catch (error) { }
    }
    
    // --- (v3.7) 1회성 API 호출 (스크롤 바) ---
    async function fetchMarketOverviewData() {
        try {
            const response = await fetch('/api/market_overview');
            if (!response.ok) return;
            const data = await response.json();
            if (data.status !== 'OK' || !scrollingBarTrack) return;

            let contentHtml = ''; 

            // 헬퍼 함수: 티커 아이템 HTML 생성
            const createItemHtml = (name, value, change) => {
                let changeClass = 'change-zero';
                let sign = '';
                if (change > 0) {
                    changeClass = 'change-positive';
                    sign = '+';
                } else if (change < 0) {
                    changeClass = 'change-negative';
                }
                
                let valueStr = (typeof value === 'number') ? value.toFixed(2) : (value || 'N/A');
                let changeStr = (typeof change === 'number') ? `${sign}${change.toFixed(2)}%` : '0.00%';

                return `
                    <div class="scroll-item">
                        <span class="name">${name}</span>
                        <span class="value">${valueStr}</span>
                        <span class="${changeClass}">${changeStr}</span>
                    </div>
                `;
            };

            // (v3.4) Top Gainers 추가
            data.gainers.forEach(t => {
                contentHtml += createItemHtml(t.ticker, t.day.c, t.todaysChangePerc);
            });

            // (v3.4) Top Losers 추가
            data.losers.forEach(t => {
                contentHtml += createItemHtml(t.ticker, t.day.c, t.todaysChangePerc);
            });

            // (v3.0) 무한 루프를 위해 내용을 2번 복제해서 삽입
            scrollingBarTrack.innerHTML = contentHtml + contentHtml;

        } catch (error) { 
            if(scrollingBarTrack) scrollingBarTrack.innerHTML = '<div class="scroll-item">Market Data Load Failed...</div>'; 
        }
    }
    
    // --- (v3.11) 5초마다 데이터 새로고침 (DB) ---
    async function fetchCommunityPosts() {
        try {
            const response = await fetch('/api/posts');
            if (!response.ok) return;
            const data = await response.json();
            if (data.status !== 'OK') return;
            
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
                communityFeedContainer.innerHTML = '<div class="empty-state">No posts yet. Be the first!</div>';
            }
        } catch (error) { }
    }
    
    // --- (v3.0) 모달 제어 함수 ---
    
        currentModalTicker = ticker;
        modal.style.display = 'block';
        modalTickerName.textContent = ticker;
        showModalTab('info', null, true); 
        window.openTickerModal = async function(ticker) {
        document.getElementById('tab-info').innerHTML = '<p>Loading company info...</p>';
        document.getElementById('tab-quote').innerHTML = '<p>Loading real-time quote...</p>';
        document.getElementById('tab-financials').innerHTML = '<p>Loading financials summary...</p>';
        document.getElementById('chart-container').innerHTML = '<p style="padding-left:24px;">Loading 1-min chart...</p>';
        
        // (API 1: 호가)
        try {
            const quoteResponse = await fetch(`/api/quote/${ticker}`);
            const quoteData = await quoteResponse.json();
            renderQuoteTab(quoteData);
        } catch (e) {
            document.getElementById('tab-quote').innerHTML = `<p style="color: red;">Quote load failed: ${e.message}</p>`; 
        }

        // (API 2: 상세 정보 + 재무)
        try {
            const detailsResponse = await fetch(`/api/details/${ticker}`);
            const detailsData = await detailsResponse.json();
            
            if (detailsData.status === 'OK') {
                const d = detailsData.results;
                document.getElementById('tab-info').innerHTML = `
                    <div class="company-details">
                        <img src="${d.logo_url}" alt="${d.name} logo" onerror="this.src='https://placehold.co/60x60/f0f0f0/999?text=N/A'"> 
                        <div class="info">
                            <h3>${d.name}</h3>
                            <div class="industry">${d.industry || 'N/A'}</div>
                        </div>
                    </div>
                    <div class="company-description">${d.description}</div>`;
                
                const f = detailsData.results.financials;
                const formatNum = (n) => (typeof n === 'number') ? n.toLocaleString() : 'N/A';
                const formatRatio = (n) => (typeof n === 'number') ? n.toFixed(2) : 'N/A';
                document.getElementById('tab-financials').innerHTML = `
                    <div class="quote-line"><span>Market Cap</span><span>$${formatNum(f.market_cap)}</span></div>
                    <div class="quote-line"><span>P/E Ratio</span><span>${formatRatio(f.pe_ratio)}</span></div>
                    <div class="quote-line"><span>P/S Ratio</span><span>${formatRatio(f.ps_ratio)}</span></div>
                    <div class="quote-line"><span>Dividend Yield</span><span>${formatRatio(f.dividend_yield)} %</span></div>`;
            } else {
                document.getElementById('tab-info').innerHTML = `<p style="color: red;">Could not load company info.</p>`;
                document.getElementById('tab-financials').innerHTML = `<p style="color: red;">Could not load financials.</p>`;
            }
        } catch (e) {
            document.getElementById('tab-info').innerHTML = `<p style="color: red;">Info load failed: ${e.message}</p>`; 
        }

        // (API 3: 차트)
        try {
            const chartResponse = await fetch(`/api/chart_data/${ticker}`);
            const chartData = await chartResponse.json();
            drawChart(chartData); 
        } catch (e) {
            document.getElementById('chart-container').innerHTML = `<p style="color: red;">Chart load failed: ${e.message}</p>`; 
        }
        
        startModalAutoRefresh(ticker);
    }
    
    // --- (v3.0) 모달 닫기 (차트 리소스 해제) ---
    function closeModal() {
        modal.style.display = 'none';
        stopModalAutoRefresh();
        
        if (lightweightChart) {
            lightweightChart.remove();
            lightweightChart = null;
        }
        candleSeries = null;
        currentModalTicker = null;
        document.getElementById('chart-container').innerHTML = ''; 
    }

    modalCloseBtn.onclick = closeModal;
    window.onclick = function(event) {
        if (event.target == modal) {
            closeModal();
        }
    }

    // --- (v3.0) 모달 탭 전환 ---
    window.showModalTab = function(tabName, clickedButton, isDefault = false) {
        document.querySelectorAll('.tab-content').forEach(tab => {
            tab.style.display = 'none';
        });
        document.querySelectorAll('.tab-button').forEach(btn => {
            btn.classList.remove('active');
        });
        document.getElementById(`tab-${tabName}`).style.display = 'block';
        if(isDefault) {
            document.querySelector('.tab-button[onclick*="\'info\'"]').classList.add('active');
        } else if (clickedButton) {
            clickedButton.classList.add('active');
        }
    }
    
    // --- (v11.0) 차트 그리기 (CSS 변수 적용) ---
    function drawChart(chartData) {
        const chartContainer = document.getElementById('chart-container');
        chartContainer.innerHTML = ''; 

        if (lightweightChart) {
            lightweightChart.remove();
            lightweightChart = null;
        }
        candleSeries = null; 

        if (chartData.status !== 'OK' || chartData.results.length === 0) {
            chartContainer.innerHTML = '<p style="padding-left:24px; color: red;">Could not load chart data.</p>';
            return;
        }

        const { createChart } = window.LightweightCharts;

        // (v11.0) CSS 변수에서 차트 색상 가져오기
        const style = getComputedStyle(document.body);
        const chartBackgroundColor = style.getPropertyValue('--bg-card').trim() || '#ffffff';
        const chartTextColor = style.getPropertyValue('--text-primary').trim() || '#191f28';
        const chartGridColor = style.getPropertyValue('--border-secondary').trim() || '#f0f0f0';
        const chartUpColor = style.getPropertyValue('--accent-positive').trim() || '#4a7c59';
        const chartDownColor = style.getPropertyValue('--accent-negative').trim() || '#5a8bde';


        lightweightChart = createChart(chartContainer, {
            width: chartContainer.clientWidth, 
            height: 350,
            layout: { 
                backgroundColor: chartBackgroundColor, // (v11.0) CSS 변수
                textColor: chartTextColor // (v11.0) CSS 변수
            },
            grid: { 
                vertLines: { color: chartGridColor }, // (v11.0) CSS 변수
                horzLines: { color: chartGridColor }  // (v11.0) CSS 변수
            },
            timeScale: { timeVisible: true, secondsVisible: false },
            attribution: { enabled: false }
        });

        candleSeries = lightweightChart.addCandlestickSeries({
            upColor: chartUpColor, // (v11.0) CSS 변수 (Sage Green)
            downColor: chartDownColor, // (v11.0) CSS 변수 (Soft Blue)
            borderVisible: false,
            wickUpColor: chartUpColor,
            wickDownColor: chartDownColor
        });

        candleSeries.setData(chartData.results);
        lightweightChart.timeScale().fitContent();
    }
    
    // --- (v3.0) 호가 탭 렌더링 ---
    function renderQuoteTab(quoteData) {
        if (quoteData.status !== 'error') {
            document.getElementById('tab-quote').innerHTML = `
                <div class="quote-line"><span>Bid Price</span><span>$${quoteData.bid_price}</span></div>
                <div class="quote-line"><span>Bid Size</span><span>${quoteData.bid_size}</span></div>
                <div class="quote-line"><span>Ask Price</span><span>$${quoteData.ask_price}</span></div>
                <div class="quote-line"><span>Ask Size</span><span>${quoteData.ask_size}</span></div>`;
        } else {
            document.getElementById('tab-quote').innerHTML = `<p style="color: red;">Could not load quote data.</p>`;
        }
    }

    // --- (v3.0) 모달 5초 새로고침 ---
    function startModalAutoRefresh(ticker) {
        stopModalAutoRefresh(); 
        
        modalRefreshInterval = setInterval(async () => {
            if (!modal.style.display || modal.style.display === 'none' || currentModalTicker !== ticker) {
                stopModalAutoRefresh();
                return;
            }
            
            try {
                // (호가 업데이트)
                try {
                    const quoteResponse = await fetch(`/api/quote/${ticker}`);
                    const quoteData = await quoteResponse.json();
                    if (modal.style.display === 'block') {
                        renderQuoteTab(quoteData);
                    }
                } catch (e) {}
                
                // (차트 업데이트)
                try {
                    const chartResponse = await fetch(`/api/chart_data/${ticker}`);
                    const chartData = await chartResponse.json();
                    
                    if (chartData.status === 'OK' && candleSeries && lightweightChart) {
                        candleSeries.setData(chartData.results);
                        lightweightChart.timeScale().fitContent();
                    }
                } catch (e) {}
            } catch (e) { }
        }, 5000);
    }
    
    function stopModalAutoRefresh() {
        if (modalRefreshInterval) {
            clearInterval(modalRefreshInterval);
        }
        modalRefreshInterval = null;
    }
    
    // --- (v3.11) 커뮤니티 폼 제출 ---
    postSubmitBtn.addEventListener('click', async function() {
        const author = postForm.elements['author'].value;
        const content = postForm.elements['content'].value;
        
        if (!content || !author) {
            console.warn('Name and content are required.');
            return;
        }
        
        try {
            const response = await fetch('/api/posts', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ author: author, content: content }),
            });
            
            const result = await response.json();
            
            if (result.status === 'OK') {
                postForm.elements['content'].value = '';
                fetchCommunityPosts();
            } else {
                console.error(`Error: ${result.message}`);
            }
        } catch (error) {
            console.error(`Post failed: ${error.message}`);
        }
    });

    // --- 11. 프로그램 시작 (v3.7 기준) ---
    setInterval(() => {
        fetchDashboardData();   
        fetchCommunityPosts();  
    }, 5000);

    fetchMarketOverviewData(); // (API 1번 호출)

    fetchDashboardData();
    fetchCommunityPosts();
    
    // --- PWA 서비스 워커 등록 ---
    // ✅ [수정 3] 'register' 성공 직후에 알림 권한을 요청하도록 수정
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js') // sw.js 파일 경로
          .then(registration => {
            console.log('✅ ServiceWorker registration successful:', registration.scope);
            
            // ✅ 서비스워커가 '등록'된 직후에 알림 권한을 요청합니다.
            requestNotificationPermission(); 

          })
          .catch(err => {
            console.log('❌ ServiceWorker registration failed:', err);
          });
      });
    }

});