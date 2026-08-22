(() => {
  'use strict';

  let installPrompt = null;
  let registration = null;
  const ua = navigator.userAgent || '';
  const isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isAndroid = /Android/i.test(ua);
  const isFirefox = /Firefox/i.test(ua);
  const isSafari = /Safari/i.test(ua) && !/Chrome|Chromium|CriOS|Edg|OPR|SamsungBrowser/i.test(ua);
  const isChromium = /Chrome|Chromium|CriOS|Edg|SamsungBrowser/i.test(ua) && !isFirefox;
  const displayMode = window.matchMedia('(display-mode: standalone)');

  function standalone() {
    return navigator.standalone === true ||
      displayMode.matches ||
      window.matchMedia('(display-mode: fullscreen)').matches ||
      window.matchMedia('(display-mode: minimal-ui)').matches ||
      document.referrer.startsWith('android-app://');
  }

  function ensureSheet() {
    let sheet = document.getElementById('pwaInstallSheet');
    if (sheet) return sheet;
    sheet = document.createElement('div');
    sheet.id = 'pwaInstallSheet';
    sheet.className = 'pwaInstallSheet';
    sheet.hidden = true;
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-labelledby', 'pwaInstallTitle');
    sheet.innerHTML = '<div class="pwaInstallPanel">' +
      '<h3 id="pwaInstallTitle">지금타를 앱으로 설치</h3>' +
      '<p id="pwaInstallLead">홈 화면에서 일반 앱처럼 바로 실행할 수 있습니다.</p>' +
      '<div id="pwaInstallSteps" class="pwaInstallSteps"></div>' +
      '<div class="pwaInstallActions"><button id="pwaInstallClose" type="button">닫기</button><button id="pwaInstallNative" class="primary" type="button" hidden>설치</button></div>' +
      '</div>';
    document.body.appendChild(sheet);
    sheet.addEventListener('click', (event) => { if (event.target === sheet) closeSheet(); });
    sheet.querySelector('#pwaInstallClose')?.addEventListener('click', closeSheet);
    sheet.querySelector('#pwaInstallNative')?.addEventListener('click', () => void triggerNativeInstall());
    return sheet;
  }

  function installInstructions() {
    if (isIOS) return 'Safari 하단의 <b>공유</b> 버튼 → <b>홈 화면에 추가</b> → <b>추가</b>를 누르세요.';
    if (isAndroid && isChromium) return '브라우저 오른쪽 위 <b>⋮</b> → <b>앱 설치</b> 또는 <b>홈 화면에 추가</b>를 누르세요.';
    if (isSafari) return 'Safari 메뉴의 <b>파일 → Dock에 추가</b>를 선택하세요.';
    if (isFirefox) return '이 브라우저는 직접 PWA 설치를 지원하지 않습니다. Android Chrome/Edge 또는 Safari에서 열어주세요.';
    return '주소창의 설치 아이콘 또는 브라우저 메뉴에서 <b>앱 설치</b>를 선택하세요.';
  }

  function openSheet() {
    if (standalone()) return;
    const sheet = ensureSheet();
    const steps = sheet.querySelector('#pwaInstallSteps');
    const nativeButton = sheet.querySelector('#pwaInstallNative');
    if (steps) steps.innerHTML = installPrompt ? '이 브라우저에서 바로 설치할 수 있습니다. 아래 <b>설치</b> 버튼을 누르세요.' : installInstructions();
    if (nativeButton) nativeButton.hidden = !installPrompt;
    sheet.hidden = false;
    document.documentElement.style.overflow = 'hidden';
  }

  function closeSheet() {
    const sheet = document.getElementById('pwaInstallSheet');
    if (sheet) sheet.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function updateButton() {
    const button = document.getElementById('pwaInstall');
    if (!button) return;
    if (standalone()) {
      button.hidden = true;
      closeSheet();
      return;
    }
    // Never hide the entry point merely because beforeinstallprompt has not fired.
    button.hidden = false;
    button.disabled = false;
    button.classList.toggle('ready', Boolean(installPrompt));
    if (installPrompt) button.textContent = '앱 설치';
    else if (isIOS) button.textContent = '홈 화면에 추가';
    else button.textContent = '앱 설치';
  }

  async function triggerNativeInstall() {
    const event = installPrompt;
    if (!event) { openSheet(); return; }
    installPrompt = null;
    closeSheet();
    try {
      await event.prompt();
      const choice = await event.userChoice.catch(() => null);
      if (choice?.outcome !== 'accepted') updateButton();
    } catch {
      updateButton();
      openSheet();
    }
  }

  function bindUI() {
    const button = document.getElementById('pwaInstall');
    if (button && !button.dataset.pwaBound) {
      button.dataset.pwaBound = '1';
      button.addEventListener('click', () => {
        if (installPrompt) void triggerNativeInstall();
        else openSheet();
      });
    }
    const updateButtonEl = document.getElementById('pwaUpdate');
    if (updateButtonEl && !updateButtonEl.dataset.pwaBound) {
      updateButtonEl.dataset.pwaBound = '1';
      updateButtonEl.addEventListener('click', () => {
        if (!registration?.waiting) return;
        updateButtonEl.disabled = true;
        updateButtonEl.textContent = '업데이트 중';
        navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), { once: true });
        registration.waiting.postMessage({ type: 'SKIP_WAITING' });
      });
    }
    updateButton();

    // Match the known-working behavior: on iOS, surface install instructions automatically.
    if (isIOS && !standalone()) {
      let alreadyShown = false;
      try { alreadyShown = sessionStorage.getItem('jigeumta_pwa_ios_sheet_v1') === '1'; } catch (_) {}
      if (!alreadyShown) {
        try { sessionStorage.setItem('jigeumta_pwa_ios_sheet_v1', '1'); } catch (_) {}
        setTimeout(openSheet, 350);
      }
    }
  }

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    installPrompt = event;
    updateButton();
    const sheet = document.getElementById('pwaInstallSheet');
    if (sheet && !sheet.hidden) openSheet();
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    updateButton();
  });
  window.addEventListener('pageshow', updateButton);
  try { displayMode.addEventListener('change', updateButton); } catch (_) { try { displayMode.addListener(updateButton); } catch (_) {} }

  async function registerWorker() {
    if (!('serviceWorker' in navigator)) return;
    try {
      const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      registration = reg;
      const updateButtonEl = document.getElementById('pwaUpdate');
      const showUpdate = () => { if (reg.waiting && navigator.serviceWorker.controller && updateButtonEl) updateButtonEl.hidden = false; };
      showUpdate();
      reg.addEventListener('updatefound', () => {
        const worker = reg.installing;
        if (!worker) return;
        worker.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller && updateButtonEl) updateButtonEl.hidden = false;
        });
      });
      const check = () => reg.update().catch(() => {});
      document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') check(); });
      window.addEventListener('online', check);
      setInterval(check, 15 * 60 * 1000);
    } catch (_) {
      // The install entry point remains visible even if SW registration fails; its sheet explains manual install.
      updateButton();
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindUI, { once: true });
  else bindUI();
  void registerWorker();
})();
