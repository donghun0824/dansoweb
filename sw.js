// 캐시 이름을 정합니다. v1, v2처럼 버전을 올리며 관리할 수 있습니다.
const CACHE_NAME = 'ichimoku-project-cache-v1';

// PWA가 설치될 때 '미리 캐시할 파일' 목록
const urlsToCache = [
  '/', // 1. 메인 페이지 (index.html)
  '/manifest.json', // 2. 매니페스트 파일
  '/static/css/style.css', // 3. CSS
  '/static/js/app.js', // 4. 자바스크립트
  '/static/images/danso_logo.png' // 5. 로고 이미지
];

// --- 1. PWA 설치 (Install) 이벤트 ---
// (이 부분은 기존과 동일합니다)
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

// --- 2. PWA 네트워크 요청 (Fetch) 이벤트 (v2 - 수정된 버전) ---
self.addEventListener('fetch', (event) => {
  const requestURL = new URL(event.request.url);

  // ✅ [수정] 앱 도메인의 리소스(same-origin)만 캐시 대상으로 삼음
  if (requestURL.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request).then((cachedResponse) => {
        // 캐시에 있으면 캐시된 것을 반환, 없으면 네트워크로 요청
        return cachedResponse || fetch(event.request)
          .catch(() => {
            // ✅ [추가] 네트워크 오류(오프라인 등)가 발생해도 충돌하지 않도록
            // 빈 응답을 보내거나, 오프라인용 페이지를 따로 반환할 수 있습니다.
            console.error('Fetch failed (offline?), returning empty response:', event.request.url);
            return new Response('', { status: 200, statusText: 'Offline Fallback' });
          });
      })
    );
  }
  // ✅ [수정] 외부 도메인 요청(예: Google Fonts)은
  // 서비스워커가 가로채지 않고 브라우저가 직접 처리하도록 무시합니다.
  // (따로 else 문이 필요 없습니다)
});