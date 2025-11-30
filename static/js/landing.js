// static/js/landing.js (V5: Premium Matte Silver & Ink Black Chart)

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
    generateChartPath(); // 리사이즈 시 차트 경로도 재생성
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
    // 시차(Parallax) 효과: 부드럽게 움직임
    vortexCenter.x = (width / 2) + (cx - width / 2) * 0.05;
    vortexCenter.y = (height / 2) + (cy - height / 2) * 0.05;
};
window.addEventListener('mousemove', handleMove);
window.addEventListener('touchmove', handleMove);

// --- 파티클 엔진: 메탈릭 더스트 (Metallic Dust) ---
class Particle {
    constructor() {
        this.init();
    }

    init() {
        this.angle = Math.random() * Math.PI * 2;
        const r = Math.random();
        this.distance = (r * r) * (Math.max(width, height) * 0.7); // 더 넓게 퍼지게
        
        this.x = vortexCenter.x + Math.cos(this.angle) * this.distance;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distance;
        
        // 크기: 아주 미세한 입자들
        this.size = Math.random() * 1.2; 
        
        // ✨ [색상 변경] 실버, 쿨 그레이, 화이트 톤 (고급스러움)
        const colors = ['#e0e0e0', '#bdbdbd', '#9e9e9e', '#757575', '#ffffff'];
        this.color = colors[Math.floor(Math.random() * colors.length)];
        
        // 속도: 아주 천천히, 우아하게
        this.speed = (100 / (this.distance + 100)) * 0.002;
        this.opacity = Math.random() * 0.5 + 0.1; // 은은하게
    }

    update() {
        this.angle += this.speed;
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

// --- 차트 엔진: 잉크 블랙 (Ink Black) & 섀도우 ---
let chartPoints = [];
let drawIndex = 0;

function generateChartPath() {
    chartPoints = [];
    const startX = 0; 
    const endX = width;
    
    // 차트는 화면 하단에서 시작해 중앙을 가로지름
    let currentY = height * 0.7;
    let currentX = startX;
    const stepX = width / 100; // 포인트 간격

    while (currentX <= endX) {
        // 우상향 트렌드 (완만하게)
        const trendUp = (currentX / width) * 1.5; 
        
        // 노이즈: 너무 뾰족하지 않고 유려하게 (Liquid 느낌)
        const noise = (Math.random() - 0.45) * 25; 
        
        currentY += noise - trendUp;
        
        // 화면 중앙 부근에 머물도록 제한
        currentY = Math.max(height * 0.3, Math.min(height * 0.8, currentY));
        
        chartPoints.push({ x: currentX, y: currentY });
        currentX += stepX;
    }
}

function drawArrowHead(x, y) {
    // 화살표 대신 끝에 작은 점(Dot)을 찍어 모던하게 마무으리
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = '#000000'; // 블랙 포인트
    ctx.fill();
    
    // 은은한 후광
    ctx.shadowBlur = 10;
    ctx.shadowColor = 'rgba(0, 0, 0, 0.3)';
    ctx.stroke();
}

function drawChart() {
    if (chartPoints.length < 2) return;

    // 1. 라인 스타일: 딥 블랙 (Deep Black)
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 3; // 너무 두껍지 않게
    ctx.strokeStyle = '#1d1d1f'; // 애플 블랙 컬러
    
    // 2. 그림자: 발광(Glow) 대신 그림자(Shadow)로 깊이감 표현
    ctx.shadowBlur = 10;
    ctx.shadowOffsetY = 5;
    ctx.shadowColor = 'rgba(0, 0, 0, 0.15)'; // 부드러운 그림자
    
    // 3. 라인 그리기
    ctx.beginPath();
    ctx.moveTo(chartPoints[0].x, chartPoints[0].y);
    
    const maxPoints = Math.floor(drawIndex);
    const visiblePoints = Math.min(maxPoints, chartPoints.length);

    // 곡선(Quadratic Curve)을 사용하여 유체처럼 부드럽게 연결
    for (let i = 1; i < visiblePoints - 1; i++) {
        const xc = (chartPoints[i].x + chartPoints[i + 1].x) / 2;
        const yc = (chartPoints[i].y + chartPoints[i + 1].y) / 2;
        ctx.quadraticCurveTo(chartPoints[i].x, chartPoints[i].y, xc, yc);
    }
    ctx.stroke();
    
    // 4. 하단 채우기 (그라데이션: 블랙 -> 투명)
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, 'rgba(0, 0, 0, 0.05)'); // 아주 연한 블랙
    gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');   // 투명

    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(chartPoints[0].x, height);
    ctx.lineTo(chartPoints[0].x, chartPoints[0].y);
    
    for (let i = 1; i < visiblePoints - 1; i++) {
        const xc = (chartPoints[i].x + chartPoints[i + 1].x) / 2;
        const yc = (chartPoints[i].y + chartPoints[i + 1].y) / 2;
        ctx.quadraticCurveTo(chartPoints[i].x, chartPoints[i].y, xc, yc);
    }
    
    if (visiblePoints > 1) {
        ctx.lineTo(chartPoints[visiblePoints-1].x, height);
        ctx.closePath();
        ctx.fill();
        
        // 끝점 장식
        if (visiblePoints >= chartPoints.length - 2) {
           drawArrowHead(chartPoints[chartPoints.length-1].x, chartPoints[chartPoints.length-1].y);
        }
    }

    // 애니메이션 진행
    if (drawIndex < chartPoints.length) {
        drawIndex += 0.4; // 천천히 우아하게 그려짐
    }
}


// --- 메인 루프 ---
const particleCount = 800; // 파티클 수를 줄여 여백의 미 강조
let particles = [];

function initParticles() {
    particles = [];
    for (let i = 0; i < particleCount; i++) {
        particles.push(new Particle());
    }
    generateChartPath();
}

function animate() {
    // 배경 지우기: 잔상 없이 깔끔하게 (투명도 조절로 잔상 남기기 가능)
    // 배경색(F5F5F7)과 일치시켜 덮어씀
    ctx.fillStyle = 'rgba(245, 245, 247, 0.4)'; 
    ctx.fillRect(0, 0, width, height);

    // 파티클 업데이트
    particles.forEach(p => p.update());

    // 차트 그리기
    drawChart();

    requestAnimationFrame(animate);
}

// 클릭 시 리셋
window.addEventListener('click', () => {
    drawIndex = 0;
    generateChartPath();
});

initParticles();
animate();