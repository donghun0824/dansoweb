// app.js (v6.1 - Animation Removed)

import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, getToken, onMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";
import { createChart } from 'https://esm.sh/lightweight-charts@4.1.1';

// 1. Firebase Config
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

// 2. 알림 권한 로직
function requestNotificationPermission() {
    if (!('Notification' in window)) {
        alert("이 브라우저는 알림을 지원하지 않습니다.");
        return;
    }
    Notification.requestPermission().then((permission) => {
        if (permission === "granted") {
            getFCMToken();
        } else {
            alert("알림 권한이 차단되었습니다. 브라우저 설정에서 허용해주세요.");
        }
    });
}

function getFCMToken() {
    const VAPID_PUBLIC_KEY = "BGMvyGLU9fapufXPNvNcyK0P0mOyhRXAeFWDlQZ4QU-sxBryPM4_K188GP9xhcqVY7vrQoJOJU5f54aeju-AzF8";
    navigator.serviceWorker.ready.then((reg) => {
        return getToken(messaging, { vapidKey: VAPID_PUBLIC_KEY, serviceWorkerRegistration: reg });
    }).then((token) => {
        if (token) {
            console.log("Token:", token);
            window.currentFCMToken = token;
            sendTokenToServer(token);
            alert("알림이 활성화되었습니다! 이제 설정을 저장할 수 있습니다.");
        }
    }).catch(console.error);
}

function sendTokenToServer(token) {
    fetch("/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: token }),
    }).catch(console.error);
}

onMessage(messaging, (payload) => {
    new Notification(payload.notification.title, { 
        body: payload.notification.body, 
        icon: "/static/images/danso_logo.png" 
    });
});

// 3. 메인 실행 로직
document.addEventListener('DOMContentLoaded', function() {

    // --- [안전장치 1] 알림 버튼 ---
    const subscribeBtn = document.getElementById('subscribe-btn');
    if (subscribeBtn) {
        subscribeBtn.addEventListener('click', requestNotificationPermission);
    }

    // --- [안전장치 2] 점수 저장 버튼 ---
    const saveScoreBtn = document.getElementById('save-score-btn');
    const minScoreInput = document.getElementById('min-score-input');

    if (saveScoreBtn && minScoreInput) {
        saveScoreBtn.addEventListener('click', async () => {
            if (!window.currentFCMToken) {
                if (Notification.permission === 'granted') {
                    getFCMToken(); return; 
                }
                alert("⚠️ 알림 권한이 없습니다. 'Alerts' 버튼을 먼저 눌러주세요.");
                return;
            }
            try {
                const response = await fetch('/api/set_alert_threshold', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: window.currentFCMToken, threshold: parseInt(minScoreInput.value) }),
                });
                if (response.ok) alert("✅ 설정 저장 완료!");
            } catch (e) { console.error(e); }
        });
    }

    // --- [안전장치 3] 모달 (STS 페이지 전용) ---
    const modal = document.getElementById('ticker-modal');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    
    if (modal && modalCloseBtn) {
        modalCloseBtn.onclick = () => { modal.style.display = 'none'; };
        window.onclick = (e) => { if (e.target == modal) modal.style.display = 'none'; };
    }

    // --- [안전장치 4] 채팅 ---
    const postForm = document.getElementById('post-form');
    const postSubmitBtn = document.getElementById('post-submit-btn');
    
    if (postForm && postSubmitBtn) {
        const contentInput = postForm.querySelector('textarea[name="content"]');
        if(contentInput) {
            contentInput.addEventListener('keydown', (e) => {
                if(e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); postSubmitBtn.click(); }
            });
        }

        postSubmitBtn.addEventListener('click', async () => {
            const author = postForm.querySelector('input[name="author"]')?.value || 'Guest';
            const content = contentInput?.value;
            if (!content) return;

            try {
                const res = await fetch('/api/posts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ author, content }),
                });
                if (res.ok) {
                    contentInput.value = '';
                    fetchCommunityPosts();
                }
            } catch (e) { console.error(e); }
        });
    }

    // --- 데이터 가져오기 ---
    async function fetchDashboardData() {
        try {
            const response = await fetch('/api/sts/status'); 
            const data = await response.json();
            
            const statusText = document.getElementById('scan-status-text');
            if (statusText && data.targets) {
                statusText.innerHTML = data.targets.length > 0 ? '<span style="color:#34c759">● Active</span>' : 'Idle';
                const countEl = document.getElementById('scan-watching-count');
                if(countEl) countEl.textContent = `${data.targets.length} Targets`;
            }

            // (A) 대시보드 테이블
            const listContainer = document.getElementById('ticker-list-container');
            if (listContainer && listContainer.tagName === 'TBODY') {
                listContainer.innerHTML = '';
                data.targets.forEach(t => {
                    const row = `
                        <tr onclick="window.location.href='/sts'" style="cursor:pointer;">
                            <td style="font-weight:bold;">${t.ticker}</td>
                            <td>$${t.price}</td>
                            <td style="color:${t.ai_prob > 0.7 ? '#34c759' : '#1D1D1F'}">${Math.round(t.ai_prob * 100)}</td>
                            <td>${t.obi.toFixed(2)}</td>
                            <td>${t.status}</td>
                        </tr>`;
                    listContainer.insertAdjacentHTML('beforeend', row);
                });
            } 
            // (B) STS 사이드바
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

            // (C) STS 중앙 테이블
            const stsTableBody = document.getElementById('sts-target-table-body');
            if (stsTableBody) {
                stsTableBody.innerHTML = '';
                data.targets.forEach(t => {
                    const row = `
                        <tr onclick="openTickerModal('${t.ticker}')" style="cursor:pointer;">
                            <td style="font-weight:bold;">${t.ticker}</td>
                            <td>$${t.price}</td>
                            <td><span class="score-badge">${Math.round(t.ai_prob * 100)}</span></td>
                            <td>${t.obi.toFixed(2)}</td>
                            <td>${t.vpin.toFixed(2)}</td>
                            <td>${t.status}</td>
                        </tr>`;
                    stsTableBody.insertAdjacentHTML('beforeend', row);
                });
            }

            // 신호 피드
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

    // --- 시장 데이터 ---
    async function fetchMarketOverview() {
        const gainersList = document.getElementById('top-gainers-list');
        const losersList = document.getElementById('top-losers-list');
        const scrollTrack = document.getElementById('scrolling-bar-track');
        
        try {
            const res = await fetch('/api/market_overview');
            const data = await res.json();
            if (data.status !== 'OK') return;

            if (scrollTrack) {
                let html = '';
                const makeItem = (t, up) => `<div style="margin-right:20px; display:inline-block;"><span style="font-weight:bold;">${t.ticker}</span> <span style="color:${up?'#34c759':'#ff3b30'}">${t.todaysChangePerc.toFixed(2)}%</span></div>`;
                data.gainers.forEach(t => html += makeItem(t, true));
                data.losers.forEach(t => html += makeItem(t, false));
                scrollTrack.innerHTML = html + html; 
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

    async function fetchCommunityPosts() {
        const container = document.getElementById('community-feed-container');
        if (!container) return;
        try {
            const res = await fetch('/api/posts');
            const data = await res.json();
            if (data.status === 'OK') {
                container.innerHTML = '';
                const myName = document.querySelector('input[name="author"]')?.value || 'Guest';
                data.posts.forEach(post => {
                    const isMe = post.author === myName;
                    const html = `
                        <div style="display:flex; flex-direction:column; align-items:${isMe?'flex-end':'flex-start'}; margin-bottom:8px;">
                            <span style="font-size:10px; color:#aaa; margin-bottom:2px;">${post.author}</span>
                            <div style="background:${isMe?'#007AFF':'#E5E5EA'}; color:${isMe?'white':'black'}; padding:6px 12px; border-radius:12px; font-size:12px; max-width:85%;">
                                ${post.content}
                            </div>
                        </div>`;
                    container.insertAdjacentHTML('beforeend', html);
                });
                container.scrollTop = container.scrollHeight;
            }
        } catch (e) {}
    }

    // 실행
    setInterval(fetchDashboardData, 2000);
    setInterval(fetchCommunityPosts, 3000);
    fetchMarketOverview();

    fetchDashboardData();
    fetchCommunityPosts();
    
    // STS 모달 (전역 함수)
    window.openTickerModal = function(ticker) {
        if (!document.getElementById('ticker-modal')) {
            window.location.href = '/sts'; return;
        }
        const m = document.getElementById('ticker-modal');
        if(m) {
            m.style.display = 'block';
            document.getElementById('modal-ticker-name').textContent = ticker;
        }
    };
});

// PWA SW
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => { navigator.serviceWorker.register('/sw.js').catch(console.error); });
}