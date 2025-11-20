// static/js/landing.js (Interactive Financial Motion Hero Effect - V3: Vortex & Neon Chart)

const canvas = document.getElementById('heroCanvas');
const ctx = canvas.getContext('2d');

let width = window.innerWidth;
let height = window.innerHeight;

// 캔버스 크기 초기 설정
canvas.width = width;
canvas.height = height;

// 창 크기 변경 시 캔버스 크기 자동 조정
window.addEventListener('resize', () => {
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width;
    canvas.height = height;
    initParticles(); 
});

// 마우스 위치 추적 (이제 Vortex의 중심이 됩니다)
let vortexCenter = { x: width / 2, y: height / 2 };

// 마우스/터치 이동 이벤트 리스너: Vortex의 중심을 마우스 위치로 업데이트
const handleMove = (e) => {
    if (e.touches && e.touches.length > 0) {
        vortexCenter.x = e.touches[0].clientX;
        vortexCenter.y = e.touches[0].clientY;
    } else {
        vortexCenter.x = e.clientX;
        vortexCenter.y = e.clientY;
    }
};

window.addEventListener('mousemove', handleMove);
window.addEventListener('touchmove', handleMove);

// --- 파티클 객체 정의 (Vortex 효과) ---
class Particle {
    constructor() {
        this.reset();
        this.color = `hsla(${Math.random() * 30 + 190}, 100%, 70%, ${Math.random() * 0.5 + 0.2})`; // 청록색 계열
    }

    reset() {
        this.x = Math.random() * width;
        this.y = Math.random() * height;
        this.size = Math.random() * 1.5 + 0.5; // 파티클 크기
        this.speed = Math.random() * 0.5 + 0.1; // 이동 속도
        this.angle = Math.random() * Math.PI * 2; // 초기 회전 각도
        this.distanceFromCenter = Math.random() * Math.min(width, height) * 0.7; // 중심으로부터의 거리
        this.fade = 1; // 사라지는 효과를 위한 투명도
    }

    draw() {
        ctx.fillStyle = this.color.replace(/, (\d+\.?\d*)\)$/, `, ${this.fade * (Math.random() * 0.5 + 0.5)})`);
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
    }

    update() {
        // Vortex 중심으로 회전하며 이동
        this.angle += this.speed * 0.005; // 회전 속도
        this.distanceFromCenter -= this.speed * 0.5; // 중심으로 빨려 들어감

        // 중심에 너무 가까워지면 리셋 (새로운 파티클 생성 효과)
        if (this.distanceFromCenter < 50) {
            this.reset();
        }

        this.x = vortexCenter.x + Math.cos(this.angle) * this.distanceFromCenter;
        this.y = vortexCenter.y + Math.sin(this.angle) * this.distanceFromCenter;
        
        this.draw();
    }
}

// --- 네온 차트 라인 그리기 데이터 ---
let neonChartPoints = []; // 차트 라인의 고정 경로
let currentChartDrawIndex = 0; // 차트 라인을 점진적으로 그릴 때 사용
const neonChartAnimationDuration = 100; // 차트가 그려지는 프레임 수
let isDrawingChart = false;

// 차트 라인 경로 정의 (고정된 우상향 라인)
function generateNeonChartPath() {
    neonChartPoints = [];
    const pointsCount = 7; // 라인의 굴곡점 개수

    const startX = width * 0.45;
    const endX = width * 0.85;
    const baseY = height * 0.85; // 라인 시작점의 Y축 (아래)
    const peakY = height * 0.15; // 라인 최고점의 Y축 (위)
    
    // 라인 경로 생성 (더 역동적인 W/M자형 우상향 패턴)
    for (let i = 0; i < pointsCount; i++) {
        const x = startX + (endX - startX) * (i / (pointsCount - 1));
        let y = baseY - (baseY - peakY) * (i / (pointsCount - 1)); // 기본 선형 상승
        
        // 역동적인 차트 굴곡 추가 (더 과감하게)
        if (i % 2 === 0) {
            y += Math.sin(i * 1.5 + Math.random() * 0.5) * 80; // 깊은 골
        } else {
            y -= Math.cos(i * 1.5 + Math.random() * 0.5) * 100; // 높은 봉우리
        }
        
        // 최종적으로는 우상향하도록 보정 (너무 내려가지 않게)
        y = Math.max(Math.min(y, baseY), peakY); 

        neonChartPoints.push({ x: x, y: y });
    }
}

// 네온 차트 라인 그리기 함수
function drawNeonChartLine() {
    if (!isDrawingChart || neonChartPoints.length === 0) return;

    ctx.strokeStyle = '#00e0ff'; // 밝은 청록색 (네온)
    ctx.lineWidth = 6; // 선 두께
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    // 네온 글로우 효과
    ctx.shadowBlur = 20; 
    ctx.shadowColor = 'rgba(0, 224, 255, 0.8)'; // 빛 번짐 색상

    ctx.beginPath();
    ctx.moveTo(neonChartPoints[0].x, neonChartPoints[0].y);

    // 차트 라인을 점진적으로 그립니다.
    const currentPointsToDraw = Math.min(neonChartPoints.length, Math.floor(neonChartPoints.length * (currentChartDrawIndex / neonChartAnimationDuration)));

    for (let i = 1; i < currentPointsToDraw; i++) {
        ctx.lineTo(neonChartPoints[i].x, neonChartPoints[i].y);
    }
    ctx.stroke();

    // 라인 아래 영역을 채워 차트의 '볼륨'을 표현 (그라데이션)
    ctx.shadowBlur = 0; // 채우기 전에 그림자 제거
    ctx.shadowColor = 'transparent';

    const gradient = ctx.createLinearGradient(0, height, 0, 0);
    gradient.addColorStop(0, 'rgba(0, 224, 255, 0)'); // 하단 투명
    gradient.addColorStop(0.5, 'rgba(0, 224, 255, 0.05)'); // 중간 반투명
    gradient.addColorStop(1, 'rgba(0, 224, 255, 0.1)'); // 상단 더 진하게

    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(neonChartPoints[0].x, neonChartPoints[0].y);
    for (let i = 1; i < currentPointsToDraw; i++) {
        ctx.lineTo(neonChartPoints[i].x, neonChartPoints[i].y);
    }
    // 아래쪽으로 선을 닫아서 영역을 만듦
    ctx.lineTo(neonChartPoints[currentPointsToDraw - 1].x, height);
    ctx.lineTo(neonChartPoints[0].x, height);
    ctx.closePath();
    ctx.fill();

    // 애니메이션 진행
    if (currentChartDrawIndex < neonChartAnimationDuration) {
        currentChartDrawIndex++;
    }
}


// --- 애니메이션 제어 ---
const numberOfParticles = 400; // 파티클 개수 증가 (Vortex 효과를 위해)
let particles = [];
let animationPhase = 0; // 0: 감시(Vortex), 1: 신호 수집, 2: 우상향(네온 차트)

function initParticles() {
    particles = [];
    for (let i = 0; i < numberOfParticles; i++) {
        particles.push(new Particle());
    }
    generateNeonChartPath(); // 차트 라인 경로 미리 생성
}

function animate() {
    // 배경 지우기 (Vortex 효과를 위해 잔상 없음)
    ctx.clearRect(0, 0, width, height); 
    //ctx.fillStyle = 'rgba(25, 31, 40, 0.1)'; // 잔상 효과 원하면 사용
    //ctx.fillRect(0, 0, width, height);

    // 각 파티클 업데이트 (항상 Vortex 움직임)
    for (let i = 0; i < particles.length; i++) {
        particles[i].update();
    }
    
    // 네온 차트 그리기 (애니메이션 페이즈 2일 때)
    if (animationPhase === 2) {
        drawNeonChartLine();
    }
    
    requestAnimationFrame(animate);
}

// --- 3단계 애니메이션 로직 (신호 발생 시) ---

// 1. 신호 수집 (Signal Collection) - Vortex가 더 강해지는 효과
function startSignalCollection() {
    if (animationPhase === 0) {
        animationPhase = 1;
        // Vortex 중심으로 파티클들이 더 빠르게 끌려들어가는 효과 추가
        particles.forEach(p => {
            p.speed *= 2; // 속도 2배 증가
        });
        console.log("-> Phase 1: Signal Collection (Vortex intensified) started.");
        
        // 2초 후 우상향 트렌드 시작
        setTimeout(startUpwardTrend, 2000);
    }
}

// 2. 우상향 트렌드 (Upward Trend) - 네온 차트 등장
function startUpwardTrend() {
    animationPhase = 2;
    isDrawingChart = true;
    currentChartDrawIndex = 0; // 차트 그리기 애니메이션 초기화
    console.log("-> Phase 2: Upward Trend (Neon Chart) started.");

    // 파티클들을 차트 주변으로 모이도록 리셋 (선택 사항 - 현재는 Vortex 유지)
    // 파티클들은 계속 Vortex 효과를 유지하면서, 그 위에 네온 차트가 그려지도록 함
    
    // 4초 후 애니메이션 초기화
    setTimeout(resetAnimation, 4000);
}

// 3. 초기화 (Reset)
function resetAnimation() {
    animationPhase = 0;
    isDrawingChart = false;
    particles.forEach(p => {
        p.reset(); // 모든 파티클 초기 상태로 리셋
    });
    console.log("-> Phase 3: Animation reset. Back to Vortex mode.");
}

// --- 최종 실행 ---

// 마우스 클릭 시 애니메이션 트리거
window.addEventListener('click', () => {
    if (animationPhase === 0) { // 감시(Vortex) 모드일 때만 클릭으로 시작
        startSignalCollection();
    }
});

initParticles(); // 파티클 및 차트 경로 초기화
animate(); // 애니메이션 시작