// 이 파일은 "최상위(Root)" 폴더에 있어야 합니다. (현재 위치가 맞습니다)

// 1. Firebase "compat" (v8) 라이브러리를 importScripts로 가져옵니다.
// (Service Worker에서 호환성을 위해 이 import 방식이 필요합니다.)
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

  // ✅ [CRITICAL FIX] 서버가 보낸 'data' 페이로드를 사용합니다.
  const data = payload.data || {};
  
  // 알림 제목과 내용을 서버가 보낸 구조화된 데이터(ticker, price, probability)로 설정합니다.
  const notificationTitle = data.title || 'Danso AI Signal';
  const notificationBody = `가격: $${data.price || 'N/A'}, AI 확률: ${data.probability || 'N/A'}%`;

  const notificationOptions = {
    // ✅ [FIX] body를 payload.data 기반으로 커스터마이징
    body: notificationBody, 
    icon: "/static/images/danso_logo.png",
    badge: "/static/images/danso_logo.png" 
  };

  self.registration.showNotification(notificationTitle, notificationOptions);
});