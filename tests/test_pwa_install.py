from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_install_button_visible_without_js():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert '<button id="pwaInstall"' in html
    button = html.split('<button id="pwaInstall"', 1)[1].split('</button>', 1)[0]
    assert ' hidden' not in button
    assert '<script defer src="/pwa.js"></script>' in html


def test_pwa_bootstrap_has_native_and_fallback_paths():
    js = (ROOT / 'pwa.js').read_text(encoding='utf-8')
    assert "beforeinstallprompt" in js
    assert "홈 화면에 추가" in js
    assert "앱 설치" in js
    assert "navigator.serviceWorker.register('/sw.js'" in js
    assert "button.hidden = false" in js


def test_service_worker_optional_assets_do_not_abort_install():
    sw = (ROOT / 'sw.js').read_text(encoding='utf-8')
    assert "cache.addAll" not in sw
    assert "OPTIONAL_SHELL.map" in sw
    assert "url.pathname.startsWith('/api/')" in sw
    assert "PWA shell request failed" in sw


def test_manifest_has_separate_maskable_icon():
    manifest = json.loads((ROOT / 'manifest.webmanifest').read_text(encoding='utf-8'))
    assert manifest['display'] == 'standalone'
    assert 'standalone' in manifest.get('display_override', [])
    purposes = [icon.get('purpose') for icon in manifest['icons']]
    assert 'any' in purposes
    assert 'maskable' in purposes
    assert (ROOT / 'icons' / 'pwa-maskable-512.png').exists()


def test_server_explicitly_serves_pwa_assets():
    server = (ROOT / 'server.py').read_text(encoding='utf-8')
    expected_routes = [
        '/manifest.webmanifest',
        '/sw.js',
        '/pwa.js',
        '/icons/pwa-192.png',
        '/icons/pwa-512.png',
        '/icons/pwa-maskable-512.png',
        '/icons/apple-touch-icon.png',
    ]
    for route in expected_routes:
        assert f'@app.get("{route}")' in server
    assert 'Service-Worker-Allowed' in server
    assert 'application/manifest+json' in server


def test_pwa_assets_referenced_by_manifest_exist():
    manifest = json.loads((ROOT / 'manifest.webmanifest').read_text(encoding='utf-8'))
    for icon in manifest['icons']:
        assert (ROOT / icon['src'].lstrip('/')).exists()
    assert (ROOT / 'sw.js').exists()
    assert (ROOT / 'pwa.js').exists()
