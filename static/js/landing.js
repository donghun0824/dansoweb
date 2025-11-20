// static/js/landing.js (Interactive Financial Motion Hero Effect)

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
    initParticles(); // 크기 변경 시 파티클 재배치
});

// 마우스 위치 추적
let mouse = { x: width / 2, y: height / 2 };

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
            const distance = Math.sqrt(dx * dx + dy * dy);
            
            // 서서히 목표에 접근
            this.x += dx * 0.05; 
            this.y += dy * 0.05;
            
            // 목표 도달 시 (불필요한 미세 움직임 제거)
            if (distance < 0.5) {
                this.x = this.targetX;
                this.y = this.targetY;
            }
        }
        
        this.draw();
    }
}

// --- 애니메이션 제어 ---
const numberOfParticles = 200; // 파티클 개수 (성능에 따라 조정)
let particles = [];
let animationPhase = 0; // 0: 감시, 1: 신호 수집, 2: 우상향

function initParticles() {
    particles = [];
    for (let i = 0; i < numberOfParticles; i++) {
        particles.push(new Particle());
    }
}

function animate() {
    // 이전 프레임 지우기 (투명도 조절로 잔상 효과 부여)
    ctx.fillStyle = 'rgba(25, 31, 40, 0.2)'; 
    ctx.fillRect(0, 0, width, height);
    
    // 각 파티클 업데이트
    for (let i = 0; i < particles.length; i++) {
        particles[i].update();
    }
    
    requestAnimationFrame(animate);
}

// --- 3단계 애니메이션 로직 (신호 발생 시) ---

// 1. 신호 수집 (Signal Collection)
function startSignalCollection() {
    if (animationPhase === 0) {
        animationPhase = 1;
        const targetPoint = { x: width * 0.7, y: height * 0.5 }; // 화면 우측 중앙
        
        // 모든 파티클의 목표 위치를 설정
        particles.forEach(p => {
            // 중앙 목표 지점 주변에 무작위로 모이도록 설정
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

    // 파티클을 우상향 차트 라인 모양으로 재배치
    const lineStart = { x: width * 0.55, y: height * 0.8 };
    const lineEnd = { x: width * 0.85, y: height * 0.2 };
    
    // 파티클을 라인 모양으로 흩뿌림
    particles.forEach((p, index) => {
        const t = index / numberOfParticles; // 0에서 1 사이의 값
        
        // 1차 함수로 라인 위치 계산 (y = mx + c)
        p.targetX = lineStart.x + (lineEnd.x - lineStart.x) * t;
        p.targetY = lineStart.y + (lineEnd.y - lineStart.y) * t;
        
        // 라인 주변에 약간의 무작위성 추가
        p.targetX += (Math.random() - 0.5) * 10;
        p.targetY += (Math.random() - 0.5) * 10;
        p.state = 'trend';
        p.color = `rgba(0, 255, 0, ${Math.random() * 0.8 + 0.5})`; // 밝은 초록색으로 강조
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
        p.x = p.targetX; // 즉시 위치 초기화
        p.y = p.targetY;
        p.state = 'watch';
        p.color = `rgba(0, 255, 100, ${Math.random() * 0.5 + 0.2})`;
        p.speedX = Math.random() * 0.2 - 0.1;
        p.speedY = Math.random() * 0.2 - 0.1;
    });
    console.log("-> Phase 3: Animation reset. Back to Watch mode.");
}

// --- 최종 실행 ---

// 마우스 클릭 시 신호 발생을 테스트하는 트리거
window.addEventListener('click', () => {
    if (animationPhase === 0) {
        startSignalCollection();
    }
});


initParticles(); // 파티클 초기화
animate(); // 애니메이션 시작