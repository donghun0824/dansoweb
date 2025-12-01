// static/js/landing.js (V4: Cinematic Galaxy & Volatile Chart - Korean Comment)

const canvas = document.getElementById('heroCanvas');
const ctx = canvas.getContext('2d');

let width = window.innerWidth;
let height = window.innerHeight;

// 1. 고해상도(Retina) 디스플레이 지원 (선명도 향상)
const dpr = window.devicePixelRatio || 1;
canvas.width = width * dpr;
canvas.height = height * dpr;
ctx.scale(dpr, dpr);

window.addEventListener('resize', () => {
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.scale(dpr, dpr);
    initParticles(); 
});

// 은하의 중심 (화면 중앙)
let vortexCenter = { x: width / 2, y: height / 2 };

const handleMove = (e) => {
    let cx, cy;
    if (e.touches && e.touches.length > 0) {
        cx = e.touches[0].clientX;
        cy = e.touches[0].clientY;
    } else {
        cx = e.clientX;
        cy = e.clientY;
    }
    // 시차(Parallax) 효과: 마우스 반대 방향으로 중심을 살짝 이동시켜 깊이감 부여
    vortexCenter.x = (width / 2) + (cx - width / 2) * 0.08;
    vortexCenter.y = (height / 2) + (cy - height / 2) * 0.08;
};
window.addEventListener('mousemove', handleMove);
window.addEventListener('touchmove', handleMove);

// --- 파티클 엔진: 로그 나선 은하 (Logarithmic Spiral) ---
class Particle {
    constructor() {
        this.init();
    }

    init() {
        // 나선 수학 공식: r = a * e^(b * theta)
        // 자연스러운 분포를 위해 각도와 거리를 랜덤화
        
        this.angle = Math.random() * Math.PI * 2;
        // 가우시안 분포 느낌으로 거리를 설정 (중심에 더 많이 모이게)
        const r = Math.random();
        this.distance = (r * r) * (Math.max(width, height) * 0.6);
        
        this.x = vortexCenter.x + Math.cos(this.angle) * this.distance;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distance;
        
        // 크기 다양화: 작은 점들로 깊이감 표현
        this.size = Math.random() * 1.5; 
        
        // 색상 팔레트: 딥 틸(Deep Teal)부터 밝은 시안/화이트까지
        const colors = ['#0f2e38', '#1c5b6e', '#00ff9d', '#ffffff', '#00e0ff'];
        this.color = colors[Math.floor(Math.random() * colors.length)];
        
        // 공전 속도: 중심에 가까울수록 빠르게 (케플러 법칙 응용)
        this.speed = (100 / (this.distance + 50)) * 0.005;
        this.opacity = Math.random() * 0.8 + 0.2;
    }

    update() {
        // 회전
        this.angle += this.speed;
        
        // 새로운 위치 계산
        this.x = vortexCenter.x + Math.cos(this.angle) * this.distance;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distance;

        this.draw();
    }

    draw() {
        ctx.globalAlpha = this.opacity;
        ctx.fillStyle = this.color;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1.0;
    }
}

// --- 네온 차트 엔진: 뾰족함(Spiky) & 발광(Glow) ---
let chartPoints = [];
let drawIndex = 0;
const drawSpeed = 2; // 프레임당 픽셀 수
let isAnimating = true; // 자동 시작

function generateChartPath() {
    chartPoints = [];
    // 왼쪽에서 시작해서 오른쪽 끝까지
    const startX = 0; 
    const endX = width;
    
    // 차트는 주로 화면 하단 2/3 지점에서 시작해 우상향
    let currentY = height * 0.8;
    let currentX = startX;
    const stepX = width / 80; // 고해상도 포인트

    while (currentX <= endX) {
        // 오른쪽으로 갈수록 위로 올라가는 경향 (강세장 표현)
        const trendUp = (currentX / width) * 2.5; 
        
        // 랜덤한 변동성(Noise) 추가 -> 뾰족한 차트 모양 생성
        const noise = (Math.random() - 0.45) * 40; // 약간 상향 편향
        
        // 트렌드 적용
        currentY += noise - trendUp;

        // 화면 밖으로 너무 나가지 않게 제한
        currentY = Math.max(height * 0.15, Math.min(height * 0.9, currentY));
        
        chartPoints.push({ x: currentX, y: currentY });
        currentX += stepX;
    }
    // 마지막 점은 화면 오른쪽 밖으로 확실히 보내기
    chartPoints.push({ x: width, y: chartPoints[chartPoints.length-1].y - 20 });
}

function drawArrowHead(x, y) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(-Math.PI / 4); // 화살표를 약간 위로 회전
    ctx.beginPath();
    ctx.moveTo(0, 0);
    ctx.lineTo(-20, 10);
    ctx.lineTo(-20, -10);
    ctx.closePath();
    ctx.fillStyle = '#00ff9d';
    ctx.shadowBlur = 30;
    ctx.shadowColor = '#00ff9d';
    ctx.fill();
    ctx.restore();
}

function drawChart() {
    if (chartPoints.length < 2) return;

    // 1. 네온 라인 설정
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 4;
    ctx.strokeStyle = '#00ff9d'; // 레퍼런스의 그 밝은 녹색
    
    // 2. 강력한 발광 효과 (3중 레이어)
    ctx.shadowBlur = 15;
    ctx.shadowColor = 'rgba(0, 255, 157, 0.8)';
    
    // 3. 라인 그리기
    ctx.beginPath();
    ctx.moveTo(chartPoints[0].x, chartPoints[0].y);
    
    // 애니메이션 인덱스에 따라 그릴 포인트 개수 결정
    const maxPoints = Math.floor(drawIndex);
    const visiblePoints = Math.min(maxPoints, chartPoints.length);

    for (let i = 1; i < visiblePoints; i++) {
        // 부드러우면서도 뾰족함을 유지하기 위해 직선 연결 사용
        const p = chartPoints[i];
        ctx.lineTo(p.x, p.y); 
    }
    ctx.stroke();
    
    // 4. 라인 아래 그라데이션 채우기
    ctx.shadowBlur = 0; // 채우기에는 그림자 제거
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, 'rgba(0, 255, 157, 0.15)');
    gradient.addColorStop(1, 'rgba(0, 255, 157, 0)');

    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(chartPoints[0].x, height); // 왼쪽 하단 시작
    ctx.lineTo(chartPoints[0].x, chartPoints[0].y); // 라인 시작점
    
    for (let i = 1; i < visiblePoints; i++) {
        ctx.lineTo(chartPoints[i].x, chartPoints[i].y);
    }
    
    if (visiblePoints > 0) {
        ctx.lineTo(chartPoints[visiblePoints-1].x, height); // 바닥으로 내리기
        ctx.closePath();
        ctx.fill();
        
        // 끝점에 화살표 머리 그리기
        if (visiblePoints === chartPoints.length) {
           drawArrowHead(chartPoints[visiblePoints-1].x, chartPoints[visiblePoints-1].y);
        }
    }

    // 애니메이션 진행
    if (drawIndex < chartPoints.length) {
        drawIndex += 0.5; // 그리는 속도 조절
    }
}


// --- 메인 루프 ---
const particleCount = 1600; // 밀도 높은 은하수
let particles = [];

function initParticles() {
    particles = [];
    for (let i = 0; i < particleCount; i++) {
        particles.push(new Particle());
    }
    generateChartPath();
}

function animate() {
    // 잔상 효과: 시네마틱한 모션 블러를 위해 반투명 배경으로 덮음
    ctx.fillStyle = 'rgba(5, 7, 10, 0.3)'; // 배경색과 일치
    ctx.fillRect(0, 0, width, height);

    // 은하수 그리기
    particles.forEach(p => p.update());

    // 차트 그리기
    drawChart();

    requestAnimationFrame(animate);
}

// 클릭 시 차트 다시 그리기
window.addEventListener('click', () => {
    drawIndex = 0;
    generateChartPath();
});

initParticles();
animate();