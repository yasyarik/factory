#!/usr/bin/env node
'use strict';

function loadGoogleApis() {
  try {
    return require('googleapis');
  } catch (_) {}
  const candidates = [
    '/var/www/my-ugc-studio-saas/node_modules/googleapis',
    '/var/www/my-ugc-studio-staging/node_modules/googleapis',
    '/var/www/api/node_modules/googleapis',
  ];
  for (const p of candidates) {
    try {
      return require(p);
    } catch (_) {}
  }
  throw new Error('googleapis module not found');
}

(async () => {
  try {
    let input = '';
    process.stdin.setEncoding('utf8');
    for await (const chunk of process.stdin) input += chunk;
    const cfg = input ? JSON.parse(input) : {};

    const keyFile = String(cfg.credentials || '').trim();
    const siteUrl = String(cfg.siteUrl || '').trim();
    const sitemaps = Array.isArray(cfg.sitemaps) ? cfg.sitemaps.map(String).filter(Boolean) : [];

    if (!keyFile) throw new Error('credentials is required');
    if (!siteUrl) throw new Error('siteUrl is required');
    if (!sitemaps.length) throw new Error('sitemaps is required');

    const { google } = loadGoogleApis();
    const auth = new google.auth.GoogleAuth({
      keyFile,
      scopes: ['https://www.googleapis.com/auth/webmasters'],
    });

    const client = await auth.getClient();
    google.options({ auth: client });
    const webmasters = google.webmasters('v3');

    const results = [];
    for (const feedpath of sitemaps) {
      try {
        await webmasters.sitemaps.submit({ siteUrl, feedpath });
        results.push({ feedpath, ok: true });
      } catch (e) {
        results.push({
          feedpath,
          ok: false,
          error: (e && e.message) ? e.message : String(e),
        });
      }
    }

    const allOk = results.every(r => r.ok);
    process.stdout.write(JSON.stringify({ success: allOk, results }));
    process.exit(allOk ? 0 : 2);
  } catch (e) {
    process.stdout.write(JSON.stringify({ success: false, error: (e && e.message) ? e.message : String(e) }));
    process.exit(1);
  }
})();
