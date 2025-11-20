// static/js/landing.js (Interactive Financial Motion Hero Effect - V2: Line Stroke Added)

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
    height = height;
    canvas.width = width;
    canvas.height = height;
    initParticles(); 
});

// 마우스 위치 추적
let mouse = { x: width / 2, y: height / 2 };
let trendPathPoints = []; // ✅ [NEW] 우상향 라인의 고정 경로를 저장할 배열

// 마우스/터치 이동 이벤트 리스너
const handleMove = (e) => {
    if (e.touches && e.touches.length > 0) {
        mouse.x = e.touches[0].clientX;
        mouse.y = e.touches[0].clientY;
    } else {
        mouse.x = e.clientX;
        mouse.y = e.clientY;
    }
};

window.addEventListener('mousemove', handleMove);
window.addEventListener('touchmove', handleMove);

// --- 파티클 객체 정의 (각 티커를 상징) ---
class Particle {
    constructor() {
        this.x = Math.random() * width;
        this.y = Math.random() * height;
        this.size = Math.random() * 2 + 0.5; // 파티클 크기
        this.color = `rgba(0, 255, 100, ${Math.random() * 0.5 + 0.2})`; // 밝은 초록색
        this.speedX = Math.random() * 0.2 - 0.1;
        this.speedY = Math.random() * 0.2 - 0.1;

        // 목표 위치 (신호 발생 지점)
        this.targetX = this.x;
        this.targetY = this.y;
        this.state = 'watch'; // 'watch', 'signal', 'trend'
    }

    draw() {
        ctx.fillStyle = this.color;
        ctx.beginPath();
        ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
        ctx.fill();
    }

    update() {
        // 1. 감시 (Watch) 상태: 불규칙한 움직임
        if (this.state === 'watch') {
            this.x += this.speedX;
            this.y += this.speedY;

            // 경계 반사
            if (this.x > width || this.x < 0) this.speedX *= -1;
            if (this.y > height || this.y < 0) this.speedY *= -1;
            
            // 마우스와의 거리 계산 (마우스가 가까우면 밀어내기)
            const dx = mouse.x - this.x;
            const dy = mouse.y - this.y;
            const distance = Math.sqrt(dx * dx + dy * dy);
            
            if (distance < 150) {
                const angle = Math.atan2(dy, dx);
                this.x -= Math.cos(angle) * (150 - distance) * 0.01;
                this.y -= Math.sin(angle) * (150 - distance) * 0.01;
            }
        } 
        
        // 2. 신호/우상향 상태: 목표를 향해 이동
        else {
            const dx = this.targetX - this.x;
            const dy = this.targetY - this.y;
            // 이동 속도 조절 (부드럽게)
            this.x += dx * 0.05; 
            this.y += dy * 0.05;
        }
        
        this.draw();
    }
}

// --- 애니메이션 제어 ---
const numberOfParticles = 200; // 파티클 개수
let particles = [];
let animationPhase = 0; // 0: 감시, 1: 신호 수집, 2: 우상향

function initParticles() {
    particles = [];
    for (let i = 0; i < numberOfParticles; i++) {
        particles.push(new Particle());
    }
}

// ✅ [NEW FUNCTION] 우상향 라인 경로를 그리는 함수 (굵고 빛나는 라인)
function drawUpwardTrendLine() {
    if (trendPathPoints.length === 0) return;

    // 1. 네온 글로우 효과
    ctx.strokeStyle = '#00ff64'; // 밝은 녹색
    ctx.lineWidth = 4; // 선 두께
    ctx.shadowBlur = 15; // 그림자 번짐 정도
    ctx.shadowColor = 'rgba(0, 255, 100, 1)'; // 그림자 색상 (네온 효과)

    ctx.beginPath();
    // 시작점 설정
    ctx.moveTo(trendPathPoints[0].x, trendPathPoints[0].y);

    // 모든 경로를 연결하여 선을 그립니다.
    for (let i = 1; i < trendPathPoints.length; i++) {
        // 캔버스 API를 사용하여 부드러운 곡선을 그릴 수도 있지만, 여기서는 직선 연결로 역동적인 차트 모양을 만듭니다.
        ctx.lineTo(trendPathPoints[i].x, trendPathPoints[i].y);
    }
    ctx.stroke();

    // 2. 그림자(글로우) 효과를 다음 요소들에 영향 주지 않도록 초기화
    ctx.shadowBlur = 0;
    ctx.shadowColor = 'transparent';
    
    // 3. (선택 사항) 라인 아래 영역을 채워 차트의 '볼륨'을 표현 (스크린샷 참고)
    // 그라데이션을 사용하거나 단색 채우기를 할 수 있습니다.
    ctx.fillStyle = 'rgba(0, 255, 100, 0.1)'; // 투명한 초록색
    ctx.lineTo(width, height); // 우측 하단
    ctx.lineTo(trendPathPoints[0].x, height); // 좌측 하단 (라인 시작점 X축)
    ctx.closePath();
    ctx.fill();
}


function animate() {
    // 이전 프레임 지우기 (투명도 조절로 잔상 효과 부여)
    ctx.fillStyle = 'rgba(25, 31, 40, 0.2)'; 
    ctx.fillRect(0, 0, width, height);
    
    // 각 파티클 업데이트
    for (let i = 0; i < particles.length; i++) {
        particles[i].update();
    }
    
    // ✅ [MODIFIED] 우상향 상태일 때만 굵은 라인을 그립니다.
    if (animationPhase === 2) {
        drawUpwardTrendLine();
    }
    
    requestAnimationFrame(animate);
}

// --- 3단계 애니메이션 로직 (신호 발생 시) ---

// 1. 신호 수집 (Signal Collection)
function startSignalCollection() {
    if (animationPhase === 0) {
        animationPhase = 1;
        // 신호 수집 목표 지점 (화면 중앙 우측)
        const targetPoint = { x: width * 0.7, y: height * 0.5 }; 
        
        particles.forEach(p => {
            p.targetX = targetPoint.x + (Math.random() - 0.5) * 50;
            p.targetY = targetPoint.y + (Math.random() - 0.5) * 50;
            p.state = 'signal';
        });

        console.log("-> Phase 1: Signal Collection started.");
        
        // 2초 후 우상향 트렌드 시작
        setTimeout(startUpwardTrend, 2000);
    }
}

// 2. 우상향 트렌드 (Upward Trend)
function startUpwardTrend() {
    animationPhase = 2;
    console.log("-> Phase 2: Upward Trend started.");

    // ✅ [MODIFIED] 고정된 라인 경로 포인트를 미리 정의합니다.
    trendPathPoints = [];
    const pointsCount = 7; // 라인의 굴곡점 개수

    const startX = width * 0.45;
    const endX = width * 0.85;
    const baseY = height * 0.9;
    const peakY = height * 0.15;
    
    // 라인 경로 생성 (W자나 M자처럼 굴곡진 우상향 패턴)
    for (let i = 0; i < pointsCount; i++) {
        const x = startX + (endX - startX) * (i / (pointsCount - 1));
        let y = baseY - (baseY - peakY) * (i / (pointsCount - 1)); // 기본 선형 상승
        
        // 역동적인 차트 굴곡 추가
        if (i % 2 === 0) {
            y += Math.sin(i * 1.5) * 50; // 깊은 골
        } else {
            y -= Math.cos(i * 1.5) * 80; // 높은 봉우리
        }
        
        // 최종적으로는 우상향하도록 보정
        y = Math.min(y, baseY - 50); // 너무 아래로 내려가지 않게 보정

        trendPathPoints.push({ x: x, y: y });
    }

    // 파티클을 라인 모양으로 흩뿌림
    particles.forEach((p, index) => {
        // 파티클을 7개의 고정점 근처에 배치
        const segment = Math.floor(index / (numberOfParticles / pointsCount));
        const pointIndex = Math.min(segment, pointsCount - 1);
        
        const targetPoint = trendPathPoints[pointIndex];
        
        p.targetX = targetPoint.x + (Math.random() - 0.5) * 40; // 무작위성 추가
        p.targetY = targetPoint.y + (Math.random() - 0.5) * 40;
        p.state = 'trend';
        p.color = `rgba(0, 255, 0, ${Math.random() * 0.8 + 0.5})`;
    });

    // 4초 후 애니메이션 초기화 (다시 감시 모드로)
    setTimeout(resetAnimation, 4000);
}

// 3. 초기화 (Reset)
function resetAnimation() {
    animationPhase = 0;
    particles.forEach(p => {
        // 기존의 불규칙한 위치로 돌아가도록 목표 재설정
        p.targetX = Math.random() * width;
        p.targetY = Math.random() * height;
        p.x = p.targetX; 
        p.y = p.targetY;
        p.state = 'watch';
        p.color = `rgba(0, 255, 100, ${Math.random() * 0.5 + 0.2})`;
        p.speedX = Math.random() * 0.2 - 0.1;
        p.speedY = Math.random() * 0.2 - 0.1;
    });
    trendPathPoints = []; // ✅ [NEW] 라인 경로 초기화
    console.log("-> Phase 3: Animation reset. Back to Watch mode.");
}

// --- 최종 실행 ---

// 마우스 클릭 시 신호 발생을 테스트하는 트리거
window.addEventListener('click', () => {
    // 신호가 없을 때만 트리거
    if (animationPhase === 0) {
        startSignalCollection();
    }
});


initParticles(); // 파티클 초기화
animate(); // 애니메이션 시작