// static/js/sts.js

document.addEventListener('DOMContentLoaded', () => {
    const grid = document.getElementById('sniper-grid');
    const logContainer = document.getElementById('trade-log');
    const timeDisplay = document.getElementById('last-update-time');

    function updateDashboard() {
        // 실제로는 fetch('/api/sts-data') 등으로 데이터를 받아와야 함
        // 여기서는 예시 데이터 구조 사용
        fetch('/api/sts/status') 
            .then(res => res.json())
            .then(data => {
                renderGrid(data.targets);
                renderLogs(data.logs);
                timeDisplay.textContent = new Date().toLocaleTimeString();
            })
            .catch(err => console.error("Data fetch error:", err));
    }

    function renderGrid(targets) {
        if (!targets || targets.length === 0) {
            grid.innerHTML = '<div class="loading-message">💤 No targets active. Scanning...</div>';
            return;
        }

        grid.innerHTML = targets.map(t => {
            const aiScore = t.ai_prob * 100;
            let statusHtml = '';
            
            if (aiScore >= 85) {
                statusHtml = `<div class="status-box status-sniper">🔥 SNIPER SIGNAL</div>`;
            } else if (aiScore >= 60) {
                statusHtml = `<div class="status-box status-watch">👀 WATCHING</div>`;
            } else {
                statusHtml = `<div class="status-box status-idle">💤 IDLE</div>`;
            }

            return `
            <div class="metric-card">
                <div class="card-header">
                    <span class="ticker-name">${t.ticker}</span>
                    <span class="ticker-price">$${t.price.toFixed(4)}</span>
                </div>
                <div class="vwap-info">VWAP Dist: <span style="color:${Math.abs(t.vwap_dist) > 2 ? '#FF4B4B':'#00FF99'}">${t.vwap_dist.toFixed(2)}%</span></div>
                
                <div class="progress-label"><span>AI Probability</span> <span>${aiScore.toFixed(1)}%</span></div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: ${aiScore}%"></div>
                </div>

                <div class="progress-label"><span>OBI (Order Book)</span> <span>${t.obi.toFixed(2)}</span></div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: ${Math.min((t.obi + 1) * 50, 100)}%"></div>
                </div>

                <div class="progress-label"><span>Tick Speed</span> <span>${t.tick_speed}</span></div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: ${Math.min(t.tick_speed, 100)}%"></div>
                </div>

                ${statusHtml}
            </div>
            `;
        }).join('');
    }

    function renderLogs(logs) {
        // 로그 렌더링 로직 (최신순)
        if (!logs) return;
        logContainer.innerHTML = logs.map(l => `
            <div class="log-entry">
                <span class="time">[${l.timestamp}]</span>
                <span class="action">${l.action}</span>
                ${l.ticker} @ $${l.price} (Score: ${l.score})
            </div>
        `).join('');
    }

    // 1초마다 갱신
    setInterval(updateDashboard, 1000);
    updateDashboard(); // 초기 실행
});