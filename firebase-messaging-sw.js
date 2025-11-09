// 이 파일은 "최상위(Root)" 폴더에 있어야 합니다. (현재 위치가 맞습니다)

// 1. Firebase "compat" (v8) 라이브러리를 importScripts로 가져옵니다.
// 'import' (v9) 문법은 서비스 워커 루트에서 작동하지 않습니다.
importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging-compat.js");

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

// 3. Firebase "compat" 버전으로 초기화
firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging(); // compat 방식

// 4. 백그라운드 메시지(푸시 알림) 처리
messaging.onBackgroundMessage((payload) => {
  console.log(
    "[firebase-messaging-sw.js] Received background message ",
    payload
  );

  // 알림을 커스터마이징합니다.
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body,
    icon: "/static/images/danso_logo.png", // (Flask의 static 경로)
    badge: "/static/images/danso_logo.png" // (안드로이드용 배지 아이콘 예시)
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});