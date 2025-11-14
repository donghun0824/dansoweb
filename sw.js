// 캐시 이름을 정의합니다. 버전을 올릴 때는 문자열을 변경하세요.
const CACHE_NAME = 'ichimoku-project-cache-v1';

// PWA가 설치될 때 미리 캐시할 파일 목록 (필요에 따라 추가/수정 가능)
const urlsToCache = [
  '/',                    // 메인 페이지 (index.html)
  '/manifest.json',       // 매니페스트
  '/static/css/style.css',// CSS
  '/static/js/app.js',    // 자바스크립트
  '/static/images/danso_logo.png' // 로고 이미지
];

// --- 1. PWA 설치(install) 이벤트 ---
// 설치 과정에서 캐시를 생성하고 지정한 파일들을 미리 저장합니다.
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Opened cache, adding files...');
        return cache.addAll(urlsToCache);
      })
      .catch(err => {
        console.error('Failed to cache files:', err);
      })
  );
});

// --- 2. PWA 활성화(activate) 이벤트 ---
// 이전 버전 캐시가 남아 있을 경우 정리해 주는 것이 좋습니다.
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keyList => {
      return Promise.all(
        keyList.map(key => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    })
  );
});

// --- 3. 네트워크 요청(fetch) 이벤트 ---
// 동일 출처(same-origin) 요청만 캐시를 우선적으로 사용하고,
// 외부 도메인 요청은 서비스워커가 건드리지 않도록 합니다.
self.addEventListener('fetch', event => {
  const requestURL = new URL(event.request.url);

  // 앱 도메인의 리소스만 처리합니다.
  if (requestURL.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then(cachedResponse => {
        // 캐시가 있으면 그 값을 반환
        if (cachedResponse) {
          return cachedResponse;
        }

        // 캐시가 없으면 네트워크로 요청
        return fetch(event.request).catch(() => {
          // 네트워크 오류나 오프라인 시에는 빈 응답 등을 반환해 앱이 충돌하지 않도록 합니다.
          console.error('Fetch failed (offline?), returning empty response:', event.request.url);
          return new Response('', { status: 200, statusText: 'Offline Fallback' });
        });
      })
    );
  }
  // 외부 도메인 요청은 서비스워커가 가로채지 않고 그대로 진행합니다.
});

// --- 4. PWA 푸시 알림 (Push) 이벤트 (v2 - 이게 진짜 최종) ---
// 백그라운드에서 FCM 메시지를 받았을 때 실행됩니다.
self.addEventListener('push', event => {
  console.log('[Service Worker] Push Received.');
  
  // 1. 전체 페이로드를 먼저 받음
  const payload = event.data.json();
  
  // 2. ✅ [수정] Firebase 'data' 메시지는 'data' 객체 안에 중첩되어 옴
  const data = payload.data; 

  const title = data.title || '새 알림';
  const options = {
    body: data.body || '새로운 내용이 있습니다.',
    icon: data.icon || '/static/images/danso_logo.png', // 알림 아이콘
    badge: '/static/images/danso_logo.png' // (안드로이드용 뱃지 아이콘)
  };

  // 알림을 화면에 띄웁니다.
  event.waitUntil(self.registration.showNotification(title, options));
});