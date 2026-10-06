// capture.js - browser side of the "site can't be reached" path. Standard browser APIs only.
//
// Run it in a browser tab that is open on the customer's homepage (paste the whole file into the browser
// tool's JavaScript runner). It fetches what extract.py reads (homepage HTML, rendered DOM, stylesheets and
// their @imports, Shopify products/collections, press pages, logo images) from inside the page, then
// downloads one JSON file named brand-pack-mirror-<host>-<time>.json. Feed it to:
//     python3 extract.py <url> --out <work-dir> --mirror <that file>
//
// Later passes: extract.py writes missing-urls.json. Set window.BP_URLS to that array, then run this file
// again; it fetches only those URLs (plus any @imports) into a new mirror file.
//
// Other hosts (a logo on a CDN) can refuse a script on the customer's site (CORS). Those come back in
// blocked_by_cors, grouped by host. For each host: open one of its URLs in the tab, set window.BP_URLS to
// that host's list, and run this file there; a page can always read its own host.
//
// Result: returns, and stores in window.__bpResult, {file, urls, ok, failed, blocked_by_cors, bytes}. The full
// mirror stays in window.__bpMirror. Set window.BP_NO_DOWNLOAD = true to skip the download (tests).
(async () => {
  const MAX_TEXT = 4e6, MAX_BIN = 3e6, TIMEOUT_MS = 20000, POOL = 6;
  const PRODUCTS = window.BP_PRODUCTS || 12;
  const list = Array.isArray(window.BP_URLS) ? window.BP_URLS : null;
  delete window.BP_URLS;
  const out = {};

  const isHttp = (u) => /^https?:$/.test(u.protocol);
  const abs = (href, base) => {
    if (!href) return null;
    try { const u = new URL(href, base); return isHttp(u) ? u.href.split('#')[0] : null; } catch (e) { return null; }
  };
  const isBinary = (ctype, url) => {
    if (/svg|xml|json|javascript|css|html|text\//i.test(ctype)) return false;
    if (/^(image|font|audio|video)\/|octet-stream|zip|pdf|postscript|illustrator/i.test(ctype)) return true;
    try { return /\.(png|jpe?g|gif|webp|avif|ico|bmp|woff2?|ttf|otf|eps|ai|zip|pdf)$/i.test(new URL(url).pathname); } catch (e) { return false; }
  };
  const toB64 = (buf) => {
    const bytes = new Uint8Array(buf); let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(s);
  };
  // A sheet the page loaded with CORS can be read back from the CSSOM when fetch() is blocked (CSP connect-src).
  const fromCssom = (url) => {
    for (const sheet of document.styleSheets) {
      if (sheet.href && sheet.href.split('#')[0] === url) {
        try { return Array.from(sheet.cssRules).map((r) => r.cssText).join('\n'); } catch (e) { return null; }
      }
    }
    return null;
  };

  async function grab(url) {
    if (!url || out[url]) return out[url];
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      const r = await fetch(url, { signal: ctl.signal });
      const ctype = r.headers.get('content-type') || '';
      const entry = { status: r.status, ctype, final: r.url || url };
      if (isBinary(ctype, url)) {
        const buf = await r.arrayBuffer();
        if (buf.byteLength > MAX_BIN) entry.error = `too large (${buf.byteLength} bytes)`;
        else { entry.b64 = true; entry.body = toB64(buf); }
      } else {
        const text = await r.text();
        entry.body = text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) : text;
      }
      out[url] = entry;
    } catch (e) {
      const css = fromCssom(url);
      const crossOrigin = new URL(url).origin !== location.origin;
      out[url] = css ? { status: 200, ctype: 'text/css', body: css, note: 'read from CSSOM; fetch failed' }
                     : { status: 0, error: String((e && e.message) || e).slice(0, 200),
                         cors: crossOrigin && !(e && e.name === 'AbortError') };
    } finally {
      clearTimeout(timer);
    }
    return out[url];
  }

  async function grabAll(urls) {
    const queue = [...new Set(urls.filter(Boolean))].filter((u) => !out[u]);
    const workers = Array.from({ length: POOL }, async () => { while (queue.length) await grab(queue.shift()); });
    await Promise.all(workers);
  }

  // extract.py follows @import inside each fetched stylesheet; fetch those too (two levels deep).
  async function followImports() {
    for (let depth = 0; depth < 2; depth++) {
      const imports = [];
      for (const [url, e] of Object.entries(out)) {
        if (url.startsWith('__') || !e.body || e.b64 || !/css/i.test(e.ctype || '')) continue;
        for (const m of e.body.matchAll(/@import\s+(?:url\()?["']?([^"')\s;]+)/g)) imports.push(abs(m[1], e.final || url));
      }
      const fresh = imports.filter((u) => u && !out[u]);
      if (!fresh.length) break;
      await grabAll(fresh);
    }
  }

  if (list) {
    await grabAll(list.map((u) => abs(u, location.href)));
  } else {
    // The rendered DOM (what the user sees) and the server HTML (what urllib would have read).
    out.__rendered__ = { ctype: 'text/html', body: document.documentElement.outerHTML };
    const home = await grab(location.origin + '/');
    const base = (home && home.final) || location.origin + '/';
    const docs = [document];
    if (home && home.body && !home.b64) docs.push(new DOMParser().parseFromString(home.body, 'text/html'));
    const siteHost = new URL(base).hostname.replace(/^www\./, '');
    const sameSite = (u) => { try { const h = new URL(u).hostname.replace(/^www\./, ''); return h === siteHost || h.endsWith('.' + siteHost); } catch (e) { return false; } };

    const urls = [];
    const pressHint = /(press|media[-_ ]?kit|brand[-_ ]?(assets|kit|guidelines|resources)|newsroom|logos?\b)/i;
    const logoHint = /(logo|brand|wordmark|logotype)/i;
    for (const d of docs) {
      d.querySelectorAll('link[href]').forEach((l) => {
        const rel = (l.getAttribute('rel') || '').toLowerCase();
        if (rel.includes('stylesheet') || rel.includes('icon') || (rel.includes('preload') && l.getAttribute('as') === 'style')) {
          urls.push(abs(l.getAttribute('href'), base));
        }
      });
      d.querySelectorAll('meta[property="og:image"], meta[name="og:image"]').forEach((m) => urls.push(abs(m.getAttribute('content'), base)));
      d.querySelectorAll('img').forEach((im) => {
        const blob = [im.getAttribute('alt'), im.getAttribute('class'), im.getAttribute('src')].join(' ');
        const chrome = im.closest('header, nav, footer, [class*="header"], [id*="header"], [class*="logo"], [id*="logo"]');
        if (!chrome && !logoHint.test(blob)) return;
        urls.push(abs(im.getAttribute('src') || im.getAttribute('data-src'), base));
        const srcset = im.getAttribute('srcset') || im.getAttribute('data-srcset');
        if (srcset) {
          const best = srcset.split(',').map((s) => s.trim().split(/\s+/)).sort((a, b) => (parseFloat(b[1]) || 0) - (parseFloat(a[1]) || 0))[0];
          if (best) urls.push(abs(best[0], base));
        }
      });
      d.querySelectorAll('use').forEach((u) => {
        const file = (u.getAttribute('href') || u.getAttribute('xlink:href') || '').split('#')[0];
        if (file) urls.push(abs(file, base));
      });
      d.querySelectorAll('a[href]').forEach((a) => {
        const u = abs(a.getAttribute('href'), base);
        if (u && sameSite(u) && pressHint.test(a.getAttribute('href') + ' ' + a.textContent)) urls.push(u);
      });
    }
    const origin = new URL(base).origin;
    for (const p of ['/press', '/pages/press', '/newsroom', '/media', '/pages/media', '/brand', '/pages/brand-assets', '/media-kit']) urls.push(origin + p);
    if (/cdn\.shopify\.com|shopify\.theme|myshopify\.com/i.test((home && home.body) || document.documentElement.outerHTML)) {
      urls.push(`${origin}/products.json?limit=${PRODUCTS}`, `${origin}/collections.json?limit=12`);
    }
    await grabAll(urls);
  }
  await followImports();

  const entries = Object.entries(out).filter(([k]) => !k.startsWith('__'));
  const failed = entries.filter(([, e]) => e.error || !(e.status >= 200 && e.status < 300)).map(([u, e]) => `${e.status || 'x'} ${u}`);
  const stamp = new Date().toISOString().replace(/\D/g, '').slice(8, 14);
  const file = `brand-pack-mirror-${location.hostname.replace(/^www\./, '')}-${stamp}.json`;
  // extract.py recomputes this; a file written or edited by hand after capture fails the check and is flagged
  const parts = [];
  for (const [u, e] of Object.entries(out)) parts.push(`${u}\n${e.status ?? ''}\n${e.body || ''}\n`);
  const digest = crypto.subtle
    ? Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(parts.join('')))),
                 (b) => b.toString(16).padStart(2, '0')).join('')
    : null;
  out.__meta__ = { tool: 'brand-pack capture.js', version: 1, page: location.href, origin: location.origin,
                   captured_at: new Date().toISOString(), pass: list ? 'url-list' : 'initial', urls: entries.length, digest };
  const blockedByCors = {};
  for (const [u, e] of entries) if (e.cors) (blockedByCors[new URL(u).origin] ||= []).push(u);
  const blob = new Blob([JSON.stringify(out)], { type: 'application/json' });
  if (!window.BP_NO_DOWNLOAD) {
    // createElementNS works when the tab shows a bare SVG or XML file (no <body>) during a CORS pass
    const a = document.createElementNS('http://www.w3.org/1999/xhtml', 'a');
    a.href = URL.createObjectURL(blob); a.download = file;
    (document.body || document.documentElement).appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 60000);
  }
  window.__bpMirror = out;
  window.__bpResult = { file, urls: entries.length, ok: entries.length - failed.length, failed: failed.slice(0, 15),
                        blocked_by_cors: blockedByCors, bytes: blob.size };
  return window.__bpResult;
})();
