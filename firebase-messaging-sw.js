// [firebase-messaging-sw.js] 최종 수정본 (데이터 메시지 처리용)

// 1. 라이브러리 임포트
importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging-compat.js");

// 2. 사용자 설정
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

// 3. 초기화
firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

// 4. 백그라운드 메시지 핸들러
messaging.onBackgroundMessage((payload) => {
  console.log("[FCM SW] 데이터 메시지 수신:", payload);

  // 🔥 [핵심] worker.py가 title과 body를 'data' 안에 숨겨 보냈습니다.
  // payload.notification이 아니라 payload.data에서 꺼내야 합니다.
  const data = payload.data || {};
  
  const title = data.title || 'Danso Alert';
  const options = {
    body: data.body || 'Check dashboard for details.',
    icon: "/static/images/danso_logo.png",
    badge: "/static/images/danso_logo.png",
    data: data // 클릭 이벤트용 데이터 전달
  };

  // 서비스 워커가 직접 알림창을 만듭니다. (브라우저 개입 차단)
  return self.registration.showNotification(title, options);
});

// 5. 알림 클릭 시 앱 열기 (UX 필수)
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  // 알림 클릭하면 메인 페이지('/')를 엽니다.
  event.waitUntil(
    clients.openWindow('/')
  );
});