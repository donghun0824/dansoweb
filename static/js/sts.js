// static/js/sts.js
// (V5.3) STS Dashboard Engine: Deep Space Visuals & Real-time Data Binding

/* ==========================================================================
   PART 1. VISUAL ENGINE (Cinematic Background)
   - 우주 배경 효과 (이전 코드 유지)
   ========================================================================== */
const canvas = document.getElementById('heroCanvas');
const ctx = canvas.getContext('2d');

let width, height;
let particles = [];
const particleCount = 200; // 파티클 수 조정
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
    constructor() {
        this.reset();
        this.y = Math.random() * height; 
    }

    reset() {
        this.x = Math.random() * width;
        this.y = Math.random() * height; 
        this.size = Math.random() * 1.5;
        this.speedX = (Math.random() - 0.5) * 0.2;
        this.speedY = (Math.random() * 0.5) + 0.1; 
        this.opacity = Math.random() * 0.5 + 0.1;
        
        // STS 시그니처 컬러
        const colors = ['#052e22', '#00ff9d', '#ffffff', '#00bcd4'];
        this.color = colors[Math.floor(Math.random() * colors.length)];
    }

    update() {
        this.x += this.speedX;
        this.y -= this.speedY; // 상승 효과

        if (this.y < 0) {
            this.y = height;
            this.x = Math.random() * width;
        }
        if (this.x < 0 || this.x > width) {
            this.x = Math.random() * width;
        }
    }

    draw() {
        ctx.fillStyle = this.color;
        ctx.globalAlpha = this.opacity;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
    }
}

function initParticles() {
    particles = [];
    for (let i = 0; i < particleCount; i++) {
        particles.push(new Star());
    }
}

function drawConnections() {
    ctx.globalAlpha = 0.05;
    ctx.strokeStyle = '#00ff9d';
    ctx.lineWidth = 0.5;

    for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
            const dx = particles[i].x - particles[j].x;
            const dy = particles[i].y - particles[j].y;
            const dist = Math.sqrt(dx * dx + dy * dy);

            if (dist < connectionDistance) {
                ctx.beginPath();
                ctx.moveTo(particles[i].x, particles[i].y);
                ctx.lineTo(particles[j].x, particles[j].y);
                ctx.stroke();
            }
        }
    }
}

function animateVisuals() {
    ctx.clearRect(0, 0, width, height);
    particles.forEach(p => {
        p.update();
        p.draw();
    });
    drawConnections();
    requestAnimationFrame(animateVisuals);
}

// 초기화 실행
initCanvas();
initParticles();
animateVisuals();

window.addEventListener('resize', () => {
    initCanvas();
    initParticles();
});


/* ==========================================================================
   PART 2. DATA ENGINE (Real-time Fetch & Render)
   - API Endpoint: /api/sts/status
   ========================================================================== */

// DOM 요소 캐싱
const els = {
    clock: document.getElementById('clock'),
    tbody: document.getElementById('target-table-body'),
    microPanel: document.getElementById('micro-panel'),
    // 리스크/퍼포먼스 패널은 필요 시 ID 추가하여 선택
};

// 1. 시계
function updateClock() {
    const now = new Date();
    els.clock.innerText = now.toLocaleTimeString('en-US', { hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// 2. 데이터 가져오기 (실제 API 호출)
async function updateDashboard() {
    try {
        // 작성하신 API 엔드포인트 호출
        const res = await fetch('/api/sts/status');
        if (!res.ok) throw new Error('Network Error');
        
        const data = await res.json();
        
        // 데이터가 없으면 종료
        if (!data || !data.targets) return;

        // (A) Top Candidates Table 렌더링
        renderTable(data.targets);
        
        // (B) 1위 종목에 대한 Microstructure 패널 업데이트
        if (data.targets.length > 0) {
            renderMicroPanel(data.targets[0]);
        }
        
        // (C) 로그 데이터 처리 (필요시 구현)
        // console.log("Recent Logs:", data.logs);

    } catch (e) {
        console.error("Dashboard Update Error:", e);
    }
}

function renderTable(targets) {
    els.tbody.innerHTML = ''; // 기존 내용 초기화

    if (targets.length === 0) {
        els.tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:20px; color:#666;">SCANNING MARKETS...</td></tr>`;
        return;
    }

    targets.forEach(item => {
        // 데이터 가공
        const price = item.price ? parseFloat(item.price).toFixed(2) : "0.00";
        // API에서 ai_score가 100점 만점으로 오는지 확인 (0~100)
        const scoreVal = item.ai_prob ? item.ai_prob * 100 : 0; 
        const score = scoreVal.toFixed(1);
        const ticker = item.ticker;
        
        // 상태별 스타일 결정
        let statusClass = 'watching';
        let statusText = 'WATCHING';
        let rowClass = ''; 

        if (item.status === 'FIRED') {
            statusClass = 'fired'; // CSS에 .fired가 없다면 aiming 스타일을 붉게 수정 필요
            statusText = 'FIRED';
            rowClass = 'style="background: rgba(255, 51, 51, 0.1);"';
        } else if (scoreVal >= 80 || item.status === 'AIMING') {
            statusClass = 'aiming';
            statusText = 'AIMING';
        }

        // 마이크로 바 길이 계산 (OBI 기준)
        let obi = item.obi || 0;
        let barWidth = Math.min(Math.abs(obi) * 50 + 50, 100); 
        
        // VPIN 리스크
        const vpinVal = item.vpin || 0;
        const riskText = calculateRiskLevel(vpinVal);

        const html = `
            <tr ${rowClass}>
                <td><span class="ticker-badge">$${ticker}</span></td>
                <td class="mono-text">$${price}</td>
                <td class="mono-text ${scoreVal >= 80 ? 'text-green' : 'text-dim'}">${score}</td>
                <td>
                    <div class="mini-bar">
                        <div style="width:${barWidth}%; opacity:${Math.min(Math.abs(obi)+0.3, 1)}"></div>
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
    // 1위 종목의 상세 정보를 왼쪽 패널에 표시
    const obi = (topItem.obi || 0).toFixed(2);
    const vpin = (topItem.vpin || 0).toFixed(2);
    const speed = topItem.tick_speed || 0;

    // 패널 HTML 직접 주입
    els.microPanel.innerHTML = `
        <div class="micro-row">
            <span>TARGET FOCUS</span>
            <span class="text-green mono-text" style="font-weight:bold;">$${topItem.ticker}</span>
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
}

// 유틸리티: VPIN 기반 리스크 레벨 텍스트
function calculateRiskLevel(vpin) {
    if (vpin > 0.6) return 'Extreme';
    if (vpin > 0.4) return 'High';
    if (vpin > 0.2) return 'Med';
    return 'Low';
}

// 엔진 시작 (1.5초마다 데이터 갱신)
setInterval(updateDashboard, 1500);
updateDashboard();