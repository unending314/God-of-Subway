const STATIC_CACHE = 'jigeumta-static-v14-11-7';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/jigeumta_logo_140.png',
  '/icons/apple-touch-icon.png',
  '/icons/pwa-192.png',
  '/icons/pwa-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(STATIC_CACHE).then(cache => cache.addAll(STATIC_ASSETS)));
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== STATIC_CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // 실시간/경로 API는 항상 네트워크를 직접 사용한다. 오래된 열차 위치를 캐시하지 않는다.
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response?.ok) {
            const copy = response.clone();
            caches.open(STATIC_CACHE).then(cache => cache.put('/index.html', copy));
          }
          return response;
        })
        .catch(() => caches.match('/index.html'))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request).then(response => {
        if (response?.ok) {
          const copy = response.clone();
          caches.open(STATIC_CACHE).then(cache => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});
