// Minimal service worker so browsers treat the page as an installable app. It caches nothing:
// live data must always come fresh from the local server.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});   // pass-through (network only)
