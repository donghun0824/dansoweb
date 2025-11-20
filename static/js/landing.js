// static/js/landing.js (Final: Perpetual Spiral Galaxy + Spiky Neon Chart)

const canvas = document.getElementById('heroCanvas');
const ctx = canvas.getContext('2d');

let width = window.innerWidth;
let height = window.innerHeight;

canvas.width = width;
canvas.height = height;

window.addEventListener('resize', () => {
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width;
    canvas.height = height;
    initParticles(); 
});

// Vortex 중심 (화면 정중앙)
let vortexCenter = { x: width / 2, y: height / 2 };

// 마우스 움직임에 따라 은하 전체가 살짝 기울어지는 입체 효과
const handleMove = (e) => {
    let clientX, clientY;
    if (e.touches && e.touches.length > 0) {
        clientX = e.touches[0].clientX;
        clientY = e.touches[0].clientY;
    } else {
        clientX = e.clientX;
        clientY = e.clientY;
    }
    // 마우스 반대 방향으로 아주 살짝 움직여 깊이감 부여
    vortexCenter.x = (width / 2) + (clientX - width / 2) * 0.05;
    vortexCenter.y = (height / 2) + (clientY - height / 2) * 0.05;
};

window.addEventListener('mousemove', handleMove);
window.addEventListener('touchmove', handleMove);

// --- 1. 파티클 (Perpetual Spiral Galaxy) ---
class Particle {
    constructor() {
        this.init();
    }

    init(isReset = false) {
        // 나선형 팔(Spiral Arms) 효과를 위한 초기 위치 계산
        // 은하의 팔 개수 (3~5개)
        const arms = 3; 
        const spin = Math.random() * Math.PI * 2; // 팔의 회전 각도
        
        // 중심에서 멀어질수록 각도가 휘어지도록 설정 (나선형 공식)
        // isReset이 true면(화면 밖으로 나갔다 돌아올 때) 바깥쪽에서 생성
        const armIndex = Math.floor(Math.random() * arms);
        const randomOffset = Math.random() * 0.5; // 팔 두께의 무작위성
        
        this.distance = isReset ? (Math.max(width, height) * 0.6) : (Math.random() * Math.max(width, height) * 0.6);
        this.angle = (this.distance / 200) + (armIndex / arms) * Math.PI * 2 + randomOffset;
        
        // 속도: 중심에 가까울수록 빠름 (케플러 법칙 느낌)
        this.speed = (150 / (this.distance + 50)) * 0.02 + 0.005;
        
        this.x = vortexCenter.x + Math.cos(this.angle) * this.distance;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distance;
        
        // 시각적 스타일
        this.size = Math.random() * 2 + 0.5;
        // 색상: 중심부는 밝은 흰색/청록, 외곽은 짙은 파랑/초록
        const hue = 160 + Math.random() * 40; // 160(Green) ~ 200(Cyan)
        const lightness = 50 + (200 / (this.distance + 1)); // 중심이 더 밝음
        this.color = `hsla(${hue}, 100%, ${lightness}%, ${Math.random() * 0.8 + 0.2})`;
    }

    update() {
        // 회전: 각도만 계속 증가시킴 (뺑글뺑글)
        this.angle += this.speed;
        
        // 아주 약간 안쪽으로 빨려 들어가는 효과 (선택사항, 은하 유지 위해 약하게)
        this.distance -= 0.2; 

        // 중심에 너무 가까워지면(블랙홀) 다시 바깥쪽에서 생성
        if (this.distance < 10) {
            this.init(true); // 외곽에서 리셋
        }

        this.x = vortexCenter.x + Math.cos(this.angle) * this.distance;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distance;

        this.draw();
    }

    draw() {
        ctx.fillStyle = this.color;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
    }
}

// --- 2. 네온 차트 (Spiky Arrow) ---
let neonChartPoints = [];
let currentChartDrawIndex = 0;
const neonChartAnimationDuration = 120; // 그리는 속도
let isDrawingChart = false;

function generateNeonChartPath() {
    neonChartPoints = [];
    // 톱니바퀴(Spiky) 디테일을 위해 포인트 개수 충분히 확보
    const pointsCount = 50; 

    const startX = width * 0.15; 
    const endX = width * 0.85;   
    const startY = height * 0.85;
    const endY = height * 0.15; 

    for (let i = 0; i < pointsCount; i++) {
        const progress = i / (pointsCount - 1);
        const x = startX + (endX - startX) * progress;
        
        // 기본 우상향 선형 라인
        let y = startY - (startY - endY) * progress;
        
        // 주식 차트 특유의 변동성(Noise) 추가
        // 끝으로 갈수록 변동폭이 커져서 긴장감 조성
        if (i < pointsCount - 1 && i > 0) {
            const volatility = 40 + progress * 60; 
            y += (Math.random() - 0.5) * volatility;
        }

        neonChartPoints.push({ x: x, y: y });
    }
    
    // 마지막 점은 화살표 끝을 위해 정확한 위치로 고정
    neonChartPoints[pointsCount - 1].x = endX;
    neonChartPoints[pointsCount - 1].y = endY;
}

function drawArrowHead(x, y) {
    // 화살표 머리 크기 키움
    const headSize = 30; 
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x - headSize, y + headSize * 0.8);
    ctx.lineTo(x - headSize * 0.8, y + headSize * 0.2); // 약간 안쪽으로 들어감
    ctx.lineTo(x - headSize, y - headSize * 0.5); 
    ctx.closePath();
    
    ctx.fillStyle = '#00ff64'; // 강렬한 네온 초록
    ctx.shadowBlur = 30;
    ctx.shadowColor = 'rgba(0, 255, 100, 1)';
    ctx.fill();
}

function drawNeonChartLine() {
    if (!isDrawingChart || neonChartPoints.length === 0) return;

    ctx.strokeStyle = '#00ff64'; 
    ctx.lineWidth = 5; // 선 두께 강화
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    // 강력한 글로우 효과 (네온)
    ctx.shadowBlur = 25;
    ctx.shadowColor = 'rgba(0, 255, 100, 0.9)';

    const progress = currentChartDrawIndex / neonChartAnimationDuration;
    const drawCount = Math.max(1, Math.floor(neonChartPoints.length * progress));

    ctx.beginPath();
    ctx.moveTo(neonChartPoints[0].x, neonChartPoints[0].y);
    for (let i = 1; i < drawCount; i++) {
        ctx.lineTo(neonChartPoints[i].x, neonChartPoints[i].y);
    }
    ctx.stroke();

    // 화살표 머리 그리기 (라인이 90% 이상 그려졌을 때 등장)
    if (progress > 0.95) {
        const lastPoint = neonChartPoints[neonChartPoints.length - 1];
        drawArrowHead(lastPoint.x, lastPoint.y);
    }

    // 라인 아래 그라데이션 (은은하게)
    ctx.shadowBlur = 0;
    const gradient = ctx.createLinearGradient(0, height, 0, 0);
    gradient.addColorStop(0, 'rgba(0, 255, 100, 0)');
    gradient.addColorStop(0.8, 'rgba(0, 255, 100, 0.1)');

    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(neonChartPoints[0].x, neonChartPoints[0].y);
    for (let i = 1; i < drawCount; i++) {
        ctx.lineTo(neonChartPoints[i].x, neonChartPoints[i].y);
    }
    if (drawCount > 0) {
        ctx.lineTo(neonChartPoints[drawCount - 1].x, height);
        ctx.lineTo(neonChartPoints[0].x, height);
    }
    ctx.closePath();
    ctx.fill();

    if (currentChartDrawIndex < neonChartAnimationDuration) {
        currentChartDrawIndex++;
    }
}

// --- 메인 제어 ---
// 은하수 느낌을 위해 파티클 개수 대폭 증가 (성능 괜찮음)
const particleCount = 1200; 
let particles = [];
let animationPhase = 0; // 0: Galaxy Loop, 1: Chart Draw

function initParticles() {
    particles = [];
    for (let i = 0; i < particleCount; i++) {
        particles.push(new Particle());
    }
    generateNeonChartPath();
}

function animate() {
    // 잔상 효과: 완전히 지우지 않고 반투명 검정으로 덮어서 꼬리 생성
    ctx.fillStyle = 'rgba(25, 31, 40, 0.3)'; 
    ctx.fillRect(0, 0, width, height);

    // 파티클은 항상(Always) 은하수처럼 돕니다
    particles.forEach(p => p.update());

    // 클릭하면 그 위에 차트가 그려짐
    if (animationPhase === 1) {
        drawNeonChartLine();
    }

    requestAnimationFrame(animate);
}

// 클릭 시 차트 등장 트리거
window.addEventListener('click', () => {
    if (animationPhase === 0) {
        generateNeonChartPath(); // 클릭할 때마다 새로운 랜덤 차트 생성
        animationPhase = 1;
        isDrawingChart = true;
        currentChartDrawIndex = 0;
        
        // 6초 후 차트 사라지고 은하수만 남음
        setTimeout(() => {
            animationPhase = 0;
            isDrawingChart = false;
        }, 6000);
    }
});

initParticles();
animate();