const CACHE_NAME = 'jigeumta-static-v14-11-9';
const CACHE_PREFIX = 'jigeumta-static-';
const REQUIRED_SHELL = ['/'];
const OPTIONAL_SHELL = [
  '/index.html',
  '/pwa.js',
  '/manifest.webmanifest',
  '/jigeumta_logo_140.png',
  '/icons/apple-touch-icon.png',
  '/icons/pwa-192.png',
  '/icons/pwa-512.png',
  '/icons/pwa-maskable-512.png'
];

async function installShell() {
  const cache = await caches.open(CACHE_NAME);
  // Only the app shell is mandatory. Optional assets must not abort SW installation.
  const shell = await fetch('/', { cache: 'no-store' });
  if (!shell.ok) throw new Error(`PWA shell request failed: ${shell.status}`);
  await cache.put('/', shell.clone());
  await Promise.all(OPTIONAL_SHELL.map(async (url) => {
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (response.ok) await cache.put(url, response);
    } catch (_) {}
  }));
}

self.addEventListener('install', event => {
  event.waitUntil(installShell());
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map(key => caches.delete(key))))
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
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate' || url.pathname === '/') {
    event.respondWith(
      fetch(request)
        .then(async response => {
          if (response.ok && response.type === 'basic') {
            const cache = await caches.open(CACHE_NAME);
            await cache.put('/', response.clone());
          }
          return response;
        })
        .catch(async () => (await caches.match('/')) || new Response('오프라인 상태입니다.', { status: 503 }))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(cached => cached || fetch(request).then(async response => {
      if (response.ok && response.type === 'basic') {
        const cache = await caches.open(CACHE_NAME);
        await cache.put(request, response.clone());
      }
      return response;
    }))
  );
});
