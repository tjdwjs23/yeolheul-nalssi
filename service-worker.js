const CACHE_NAME = "yeolheul-pwa-v1";

const APP_SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/icon-maskable-512.png",
  "./icons/apple-touch-icon.png"
];

const DATA_URL = new URL("./data.json", self.registration.scope).href;

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys =>
        Promise.all(
          keys
            .filter(key => key.startsWith("yeolheul-pwa-") && key !== CACHE_NAME)
            .map(key => caches.delete(key))
        )
      )
      .then(async () => {
        await self.clients.claim();
        // 첫 방문의 데이터 요청은 워커 활성화 전에 끝날 수 있다.
        await networkFirstWeather(new Request(DATA_URL));
      })
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;

  if (request.method !== "GET") return;

  const url = new URL(request.url);

  // CDN/외부 폰트는 브라우저 기본 동작에 맡긴다.
  if (url.origin !== self.location.origin) return;

  // 날씨 데이터는 항상 최신 네트워크 우선.
  if (url.pathname === new URL(DATA_URL).pathname) {
    event.respondWith(networkFirstWeather(request));
    return;
  }

  // 페이지 이동도 새 배포 내용을 먼저 확인.
  if (request.mode === "navigate") {
    event.respondWith(networkFirstPage(request));
    return;
  }

  // 나머지 로컬 정적 리소스는 캐시 우선.
  event.respondWith(cacheFirst(request));
});

async function networkFirstWeather(request) {
  const cache = await caches.open(CACHE_NAME);

  try {
    const response = await fetch(request, { cache: "no-store" });

    if (!response.ok) throw new Error(`Weather HTTP ${response.status}`);
    const data = await response.clone().json();
    if (!data["지역들"]) throw new Error("Invalid weather data");
    await cache.put(DATA_URL, response.clone());

    return response;
  } catch (error) {
    const cached = await cache.match(DATA_URL);

    if (cached) return cached;

    return new Response(
      JSON.stringify({
        error: "offline",
        message: "저장된 날씨 데이터가 없습니다."
      }),
      {
        status: 503,
        headers: {
          "Content-Type": "application/json; charset=utf-8"
        }
      }
    );
  }
}

async function networkFirstPage(request) {
  const cache = await caches.open(CACHE_NAME);

  try {
    const response = await fetch(request);

    if (!response.ok) throw new Error(`Page HTTP ${response.status}`);
    await cache.put("./index.html", response.clone());

    return response;
  } catch (error) {
    return (
      await cache.match("./index.html") ||
      await cache.match("./")
    );
  }
}

async function cacheFirst(request) {
  const cached = await caches.match(request);

  if (cached) return cached;

  const response = await fetch(request);

  if (response.ok) {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(request, response.clone());
  }

  return response;
}
