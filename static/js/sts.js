// static/js/sts.js
// (V5.3) STS Dashboard Engine: Deep Space Visuals & Real-time Data Binding

/* ==========================================================================
   PART 1. VISUAL ENGINE (배경 효과 - 건드리지 않음)
   ========================================================================== */
const canvas = document.getElementById('heroCanvas');
const ctx = canvas.getContext('2d');
let width, height, particles = [];
const particleCount = 200;
const connectionDistance = 120;

function initCanvas() {
    const dpr = window.devicePixelRatio || 1;
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);
}

class Star {
    constructor() { this.reset(); this.y = Math.random() * height; }
    reset() {
        this.x = Math.random() * width;
        this.y = Math.random() * height;
        this.size = Math.random() * 1.5;
        this.speedX = (Math.random() - 0.5) * 0.2;
        this.speedY = (Math.random() * 0.5) + 0.1;
        this.opacity = Math.random() * 0.5 + 0.1;
        // Softened palette for premium feel
        this.color = ['#222222', '#00ff9d', '#e0e0e0', '#888888'][Math.floor(Math.random() * 4)];
    }
    update() {
        this.x += this.speedX; this.y -= this.speedY;
        if (this.y < 0) { this.y = height; this.x = Math.random() * width; }
        if (this.x < 0 || this.x > width) this.x = Math.random() * width;
    }
    draw() {
        ctx.fillStyle = this.color; ctx.globalAlpha = this.opacity;
        ctx.beginPath(); ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2); ctx.fill();
    }
}

function initParticles() { particles = []; for (let i = 0; i < particleCount; i++) particles.push(new Star()); }
function drawConnections() {
    ctx.globalAlpha = 0.05; ctx.strokeStyle = '#cccccc'; ctx.lineWidth = 0.5; // Softer connection line
    for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
            const dx = particles[i].x - particles[j].x;
            const dy = particles[i].y - particles[j].y;
            if (Math.sqrt(dx * dx + dy * dy) < connectionDistance) {
                ctx.beginPath(); ctx.moveTo(particles[i].x, particles[i].y); ctx.lineTo(particles[j].x, particles[j].y); ctx.stroke();
            }
        }
    }
}
function animateVisuals() { ctx.clearRect(0, 0, width, height); particles.forEach(p => { p.update(); p.draw(); }); drawConnections(); requestAnimationFrame(animateVisuals); }

initCanvas(); initParticles(); animateVisuals();
window.addEventListener('resize', () => { initCanvas(); initParticles(); });

/* ==========================================================================
   PART 2. DATA ENGINE (여기가 핵심 수정 파트)
   ========================================================================== */

const els = {
    clock: document.getElementById('clock'),
    tbody: document.getElementById('target-table-body'),
    microPanel: document.getElementById('micro-panel'),
    signals: document.getElementById('session-signals'), // 추가
    winrate: document.getElementById('session-winrate')  // 추가
};

// 1. 시계
function updateClock() {
    if(els.clock) els.clock.innerText = new Date().toLocaleTimeString('en-US', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// 2. 데이터 가져오기 (Polling 방식: 1.5초마다 DB 조회)
async function updateDashboard() {
    try {
        const res = await fetch('/api/sts/status');
        if (!res.ok) throw new Error('Network Error');
        
        const data = await res.json();
        
        // 데이터가 없거나 비어있으면 리턴
        if (!data || !data.targets) return;

        // (A) Top Candidates Table 렌더링
        renderTable(data.targets);
        
        // (B) 1위 종목 패널 업데이트
        if (data.targets.length > 0) {
            renderMicroPanel(data.targets[0]);
        }
        
        // (C) 상단 정보창 업데이트
        if (els.signals) els.signals.innerText = data.targets.length;
        if (els.winrate) els.winrate.innerText = "RUNNING";

    } catch (e) {
        console.error("Dashboard Sync Error:", e);
    }
}

function renderTable(targets) {
    if (!els.tbody) return;
    els.tbody.innerHTML = ''; 

    if (targets.length === 0) {
        els.tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:20px; color:#888;">SCANNING MARKETS...</td></tr>`;
        return;
    }

    targets.forEach(item => {
        const price = item.price ? parseFloat(item.price).toFixed(2) : "0.00";
        // item.ai_score가 DB에 저장된 값 (예: 82.3)
        let rawScore = item.ai_score || item.ai_prob || 0; 
        const scoreVal = parseFloat(rawScore);
        const score = scoreVal.toFixed(1);
        const ticker = item.ticker;
        
        let statusClass = 'watching';
        let statusText = 'WATCHING';
        let rowClass = ''; 

        // Modified visual states for cleaner premium feel
        if (item.status === 'FIRED') {
            statusClass = 'fired';
            statusText = 'FIRED';
            // Softer emergency red, less aggressive
            rowClass = 'style="background: rgba(255, 50, 50, 0.05);"';
        } else if (scoreVal >= 60 || item.status === 'AIMING') {
            statusClass = 'aiming';
            statusText = 'AIMING';
        }

        const obi = item.obi || 0;
        const vpinVal = item.vpin || 0;
        const barWidth = Math.min(Math.abs(obi) * 50 + 50, 100);
        
        // 리스크 텍스트 - Cleaned up styling logic
        let riskText = 'Low';
        if (vpinVal > 0.6) riskText = 'Extreme';
        else if (vpinVal > 0.4) riskText = 'High';
        else if (vpinVal > 0.2) riskText = 'Med';

        // Updated Table Row Render - Clean contrast, minimal neon
        const html = `
            <tr ${rowClass}>
                <td><span class="ticker-badge">$${ticker}</span></td>
                <td class="mono-text">$${price}</td>
                <td class="mono-text ${scoreVal >= 80 ? 'text-green' : 'text-dim'}">${score}</td>
                <td>
                    <div class="mini-bar">
                        <div style="width:${barWidth}%; opacity:${Math.min(Math.abs(obi)+0.5, 1)}"></div>
                    </div>
                </td>
                <td><span class="text-dim">${riskText}</span></td>
                <td><span class="mono-text">VWAP ${item.vwap_dist ? item.vwap_dist.toFixed(2) : '0.0'}%</span></td>
                <td><span class="status-badge ${statusClass}">${statusText}</span></td>
            </tr>
        `;
        els.tbody.insertAdjacentHTML('beforeend', html);
    });
}

function renderMicroPanel(topItem) {
    if (!els.microPanel) return;

    const obi = (topItem.obi || 0).toFixed(2);
    const vpin = (topItem.vpin || 0).toFixed(2);
    const speed = topItem.tick_speed || 0;

    // Updated Micro Panel - Removed glow text classes, using clean semantic styles
    els.microPanel.innerHTML = `
        <div class="micro-row">
            <span>TARGET FOCUS</span>
            <span class="text-highlight mono-text" style="font-weight:700;">$${topItem.ticker}</span>
        </div>
        <div class="micro-row">
            <span>OBI (Order Flow)</span>
            <div class="progress-wrapper">
                <span class="val-text mono-text ${obi > 0 ? 'text-green' : 'text-warn'}">${obi}</span>
                <div class="progress-bg"><div class="progress-fill ${obi > 0 ? 'green' : 'warn'}" style="width: ${Math.min(Math.abs(obi)*100, 100)}%;"></div></div>
            </div>
        </div>
        <div class="micro-row">
            <span>VPIN (Toxicity)</span>
            <div class="progress-wrapper">
                <span class="val-text mono-text text-dim">${vpin}</span>
                <div class="progress-bg"><div class="progress-fill warn" style="width: ${vpin * 100}%;"></div></div>
            </div>
        </div>
        <div class="micro-item mt-15">
            <span>Tape Speed</span>
            <span class="mono-text text-highlight">⚡ ${speed} ticks/s</span>
        </div>
    `;

    // Risk Engine 패널 값 채우기 (HTML ID가 존재할 경우에만)
    const price = parseFloat(topItem.price || 0);
    const atrSim = price * 0.01; 
    
    // 안전하게 요소 찾기
    const setVal = (id, val) => { const e = document.getElementById(id); if(e) e.innerText = val; };
    
    setVal('risk-entry', '$' + price.toFixed(2));
    
    const targetEl = document.getElementById('risk-target');
    if (targetEl) {
        targetEl.innerText = "TRAILING 🚀"; // Strategic indicator
        targetEl.style.color = "#00ff9d";   // Kept accent green for key signal
    }

    setVal('risk-stop', '$' + (price - atrSim).toFixed(2));
    
    const probEl = document.getElementById('risk-prob');
    if (probEl) {
        const prob = (topItem.ai_score || 0);
        probEl.innerText = prob.toFixed(1) + '%';
        // Clean conditional styling: Green only for high confidence
        probEl.className = prob >= 80 ? "value mono-text text-green" : "value mono-text";
    }
}

// 3. 엔진 가동 (가장 중요한 부분)
// 1.5초마다 API를 때려서 화면을 갱신합니다.
setInterval(updateDashboard, 1500);
updateDashboard(); // 즉시 1회 실행