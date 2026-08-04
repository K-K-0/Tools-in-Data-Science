const { chromium } = require('playwright');
const path = require('path');

// ---------- CONFIG: adjust to match your actual markup ----------
const FILE_PATH = 'file:///home/k-k/Downloads/q-playwright-shadow-incident-audit-server.html';
const START = new Date('2026-03-19T00:00:00Z');
const END   = new Date('2026-05-14T00:00:00Z'); // exclusive
// ------------------------------------------------------------------

function normalizeDuration(raw) {
  if (raw == null) return 0;
  if (typeof raw === 'number') return raw;
  const s = String(raw).trim();

  // ISO-8601 duration: PT1H30M, PT45M, PT2H
  const iso = s.match(/^PT(?:(\d+)H)?(?:(\d+)M)?$/i);
  if (iso) return (+(iso[1] || 0)) * 60 + (+(iso[2] || 0));

  // "Hh Mm" format: "1h 30m", "2h", "45m"
  const hm = s.match(/^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?$/i);
  if (hm && (hm[1] || hm[2])) return (+(hm[1] || 0)) * 60 + (+(hm[2] || 0));

  // plain minutes, possibly as a numeric string
  const num = Number(s.replace(/[^0-9.]/g, ''));
  return Number.isFinite(num) ? num : 0;
}

function normalizeImpact(raw) {
  if (raw == null) return 0;
  if (typeof raw === 'number') return raw;
  const num = Number(String(raw).replace(/[^0-9.]/g, ''));
  return Number.isFinite(num) ? num : 0;
}

async function extractRecordsFromPage(page) {
  // Runs in-browser: walks two levels of open shadow roots,
  // collects .record[data-active="true"], and pulls fields
  // either from data-* attributes or from a JSON blob in the element.
  return await page.evaluate(() => {
    const out = [];

    function parseRecord(el) {
      // Prefer a JSON payload if present (common pattern: data-payload='{...}')
      if (el.dataset && el.dataset.payload) {
        try { return JSON.parse(el.dataset.payload); } catch (e) { /* fall through */ }
      }
      // Fallback: read individual data-* attributes directly
      const d = el.dataset || {};
      return {
        event_id: d.eventId,
        incident_id: d.incidentId,
        revision: d.revision !== undefined ? Number(d.revision) : undefined,
        updated_at: d.updatedAt,
        team: d.team,
        severity: d.severity,
        status: d.status,
        duration: d.duration,
        impact: d.impact,
      };
    }

    function walk(root) {
      root.querySelectorAll('*').forEach(el => {
        if (el.shadowRoot) {
          // level with .record directly inside, or nested another level
          el.shadowRoot.querySelectorAll('.record[data-active="true"]').forEach(rec => {
            out.push(parseRecord(rec));
          });
          walk(el.shadowRoot); // recurse for the second nesting level
        }
      });
    }

    walk(document);
    return out;
  });
}

async function isNextDisabled(page) {
  return await page.$eval('#next-page', el => {
    return el.disabled === true ||
           el.hasAttribute('disabled') ||
           el.getAttribute('aria-disabled') === 'true';
  }).catch(() => true); // if #next-page doesn't exist, treat as done
}

async function main() {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(FILE_PATH);

  const all = [];
  let pageCount = 0;

  while (true) {
    await page.waitForSelector('body[data-ready="true"]', { timeout: 15000 });

    const records = await extractRecordsFromPage(page);
    all.push(...records);
    pageCount++;

    const disabled = await isNextDisabled(page);
    if (disabled) break;

    // Reset readiness flag detection: click, then wait for it to flip
    await page.click('#next-page');
    // Give the SPA a moment to invalidate the ready flag before re-waiting
    await page.waitForFunction(() => {
      return document.body.getAttribute('data-ready') !== 'true';
    }, { timeout: 5000 }).catch(() => {}); // some implementations may not flicker
  }

  await browser.close();

  console.error(`Crawled ${pageCount} pages, ${all.length} raw records`);

  // ---- Step 1: dedup replays by event_id ----
  const seen = new Set();
  const deduped = all.filter(r => {
    if (!r.event_id) return true; // keep if no id (shouldn't happen, but don't silently drop)
    if (seen.has(r.event_id)) return false;
    seen.add(r.event_id);
    return true;
  });
  console.error(`After event_id dedup: ${deduped.length}`);

  // ---- Step 2: collapse to latest revision per incident_id (BEFORE filters) ----
  const byIncident = new Map();
  for (const r of deduped) {
    const cur = byIncident.get(r.incident_id);
    if (!cur) { byIncident.set(r.incident_id, r); continue; }
    const rRev = Number(r.revision), curRev = Number(cur.revision);
    if (rRev > curRev) {
      byIncident.set(r.incident_id, r);
    } else if (rRev === curRev) {
      if (new Date(r.updated_at) > new Date(cur.updated_at)) {
        byIncident.set(r.incident_id, r);
      }
    }
  }
  const latest = [...byIncident.values()];
  console.error(`After revision collapse: ${latest.length} unique incidents`);

  // ---- Step 3: apply business filters ----
  const qualifying = latest.filter(r => {
    if (r.team !== 'Beacon') return false;
    if (r.severity !== 'S1' && r.severity !== 'S2') return false;
    if (r.status !== 'RESOLVED') return false;
    const t = new Date(r.updated_at);
    if (!(t >= START && t < END)) return false;
    return true;
  });
  console.error(`Qualifying incidents: ${qualifying.length}`);

  // ---- Step 4: normalize + aggregate ----
  const durations = qualifying
    .map(r => normalizeDuration(r.duration))
    .sort((a, b) => a - b);
  const impacts = qualifying.map(r => normalizeImpact(r.impact));

  const n = durations.length;
  const totalDowntime = durations.reduce((a, b) => a + b, 0);
  const totalImpact = Math.round(impacts.reduce((a, b) => a + b, 0) * 100) / 100;

  let p95 = null;
  if (n > 0) {
    const rank = Math.ceil(0.95 * n); // 1-based nearest-rank
    p95 = durations[rank - 1];
  }

  const result = {
    qualifying_incident_count: n,
    total_downtime_minutes: totalDowntime,
    total_impact_usd: totalImpact,
    duration_p95_minutes: p95
  };

  console.log(JSON.stringify(result, null, 2));
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});