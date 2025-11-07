// 캐시 이름을 정합니다. v1, v2처럼 버전을 올리며 관리할 수 있습니다.
const CACHE_NAME = 'ichimoku-project-cache-v1';

// PWA가 설치될 때 '미리 캐시할 파일' 목록
// 이 파일들이 오프라인에서도 작동하도록 보장합니다.
const urlsToCache = [
  '/', // 1. 메인 페이지 (index.html)
  '/manifest.json', // 2. 매니페스트 파일
  '/static/css/style.css', // 3. CSS
  '/static/js/app.js', // 4. 자바스크립트
  '/static/images/danso_logo.png' // 5. 로고 이미지 (경로 확인!)
  // 여기에 오프라인 시 꼭 필요한 다른 이미지나 폰트 파일 경로를 추가하세요.
];

// --- 1. PWA 설치 (Install) 이벤트 ---
// 서비스 워커가 '설치'될 때 한 번 실행됩니다.
self.addEventListener('install', event => {
  // 설치가 완료될 때까지 기다립니다.
  event.waitUntil(
    // CACHE_NAME으로 캐시 저장소를 엽니다.
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Opened cache, adding files...');
        // urlsToCache에 있는 모든 파일을 다운로드해서 캐시에 저장합니다.
        return cache.addAll(urlsToCache);
      })
      .catch(err => {
        console.error('Failed to cache files:', err);
      })
  );
});

// --- 2. PWA 네트워크 요청 (Fetch) 이벤트 ---
// CSS, JS, 이미지, API 요청 등 모든 네트워크 요청이 발생할 때마다 실행됩니다.
self.addEventListener('fetch', event => {
  // 브라우저의 기본 요청/응답을 우리가 직접 제어합니다.
  event.respondWith(
    // 1. 캐시에서 먼저 찾아봅니다.
    caches.match(event.request)
      .then(response => {
        // 2. (성공) 캐시에 파일이 있다면:
        if (response) {
          console.log('Cache hit:', event.request.url);
          return response; // 캐시된 파일을 즉시 반환 (오프라인 작동!)
        }

        // 3. (실패) 캐시에 파일이 없다면:
        console.log('Cache miss, fetching:', event.request.url);
        return fetch(event.request); // 원래대로 인터넷(네트워크)으로 요청
      })
  );
});