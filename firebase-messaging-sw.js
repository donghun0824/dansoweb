// 이 파일의 이름은 반드시 'firebase-messaging-sw.js'여야 합니다.
// (경로: static/firebase-messaging-sw.js)

// 1. Firebase 모듈 가져오기 (CDN)
import { initializeApp } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-app.js";
import { getMessaging, onBackgroundMessage } from "https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging.js";

// 2. ✅ 사용자님의 firebaseConfig
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

// 3. Firebase 초기화
initializeApp(firebaseConfig);
const messaging = getMessaging();

// 4. 백그라운드 메시지(푸시 알림) 처리
onBackgroundMessage(messaging, (payload) => {
  console.log(
    "[firebase-messaging-sw.js] Received background message ",
    payload
  );

  // 알림을 커스터마이징합니다.
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: "/static/images/danso_logo.png", // (Flask의 static 경로)
    badge: "/static/images/danso_badge.png" // (안드로이드용 배지 아이콘 예시)
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});