// Portfolio Control v3.7.0 uses a local Python backend and SQLite database.
// Keep the service worker intentionally cache-free so state/API responses never go stale.
self.addEventListener('install', event => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
