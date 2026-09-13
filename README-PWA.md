# 열흘날씨 PWA 적용

저장소 루트에 아래 파일/폴더를 추가하세요.

```text
yeolheul-nalssi/
├─ index.html
├─ data.json
├─ manifest.webmanifest
├─ service-worker.js
└─ icons/
   ├─ icon-192.png
   ├─ icon-512.png
   ├─ icon-maskable-512.png
   └─ apple-touch-icon.png
```

## index.html 수정 1 — <head>

현재 `<meta name="theme-color" ...>` 아래에
`pwa-head-snippet.html` 내용을 붙여 넣으세요.

## index.html 수정 2 — </body> 직전

`pwa-register-snippet.html` 내용을 붙여 넣으세요.

현재 마지막이:

```html
</script>
</body>
</html>
```

이라면 다음처럼 됩니다.

```html
</script>

<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("./service-worker.js")
      .catch(err => console.error("Service Worker 등록 실패:", err));
  });
}
</script>

</body>
</html>
```

## 설치

GitHub에 commit/push하고 GitHub Pages 배포가 끝난 뒤,
Galaxy Chrome에서 사이트를 연 다음:

Chrome 메뉴(⋮) → `앱 설치` 또는 `홈 화면에 추가`

를 누르면 됩니다.

## 동작 방식

- 주소창 없는 standalone 앱
- 기존 지역 선택 localStorage(`yeolheul-region`) 유지
- `data.json`은 네트워크 최신값 우선
- 통신 실패 시 마지막 정상 날씨 데이터 사용
- 앱 UI/아이콘 캐시
- `/yeolheul-nalssi/` GitHub Pages 하위 경로 대응

## 서비스워커 수정 후 예전 버전이 남는 경우

`service-worker.js`의

```js
const CACHE_NAME = "yeolheul-pwa-v1";
```

을 `v2`, `v3`처럼 올리면 기존 캐시를 자동 정리합니다.
