// [firebase-messaging-sw.js] 최종 수정본
// worker.py가 보낸 제목/내용을 그대로 표시하는 표준 방식

importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/9.0.0/firebase-messaging-compat.js");

// 사용자님의 Config 유지
const firebaseConfig = {
  apiKey: "AIzaSyDWDmEgyl2z6mh8-OJ4jXubROLqbPbl6wk",
  authDomain: "gen-lang-client-0379169283.firebaseapp.com",
  projectId: "gen-lang-client-0379169283",
  storageBucket: "gen-lang-client-0379169283.firebasestorage.app",
  messagingSenderId: "506115337247",
  appId: "1:506115337247:web:efe15620d3547b7255392a",
  measurementId: "G-DFFBKLCBWS"
};

firebase.initializeApp(firebaseConfig);
const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {
  console.log("[FCM SW] 백그라운드 메시지 수신:", payload);

  // 🔥 [핵심 수정] 
  // 기존처럼 data.price 등을 꺼내서 직접 조립하지 마세요!
  // worker.py가 이미 notification.title과 notification.body에
  // "BUY AAPL (Score:99)" 같은 완성된 문구를 담아서 보냈습니다.
  // 우리는 그걸 그대로 가져다 쓰기만 하면 됩니다.
  
  const notificationTitle = payload.notification.title;
  const notificationOptions = {
    body: payload.notification.body, // 서버가 보낸 내용 그대로 사용
    icon: "/static/images/danso_logo.png",
    // 클릭 시 앱으로 데이터 전달을 위해 data 객체는 유지
    data: payload.data 
  };

  return self.registration.showNotification(notificationTitle, notificationOptions);
});