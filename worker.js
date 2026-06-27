// FrodoTappins sync Worker (Cloudflare Workers + D1, free tier).
//
// Endpoints (both require the header  X-API-Key: <your key>):
//   POST /sync     body: { device_id, device_name, days: [{day,keystrokes,words,deletions,alt_tabs,power_cycles}, ...] }
//   GET  /totals   -> { global:{...}, device_count, devices:[{...}] }
//
// The API key is stored as a Worker secret (see README) and compared in a
// header. It is never read from the URL and is never logged.

const FIELDS = ["keystrokes", "words", "deletions", "alt_tabs", "power_cycles"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type,X-API-Key,Authorization",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // ---- auth ----
    const key =
      request.headers.get("X-API-Key") ||
      (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!env.API_KEY || key !== env.API_KEY) {
      return json({ error: "unauthorized" }, 401, cors);
    }

    try {
      if (request.method === "POST" && url.pathname === "/sync") {
        return await handleSync(request, env, cors);
      }
      if (request.method === "GET" && url.pathname === "/totals") {
        return await handleTotals(env, cors);
      }
      return json({ error: "not found" }, 404, cors);
    } catch (e) {
      return json({ error: String((e && e.message) || e) }, 500, cors);
    }
  },
};

function json(obj, status = 200, headers = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function clampInt(x) {
  const v = Number(x);
  return Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0;
}

async function handleSync(request, env, cors) {
  const body = await request.json();
  const deviceId = String(body.device_id || "").slice(0, 64);
  const deviceName = String(body.device_name || "").slice(0, 128);
  const days = Array.isArray(body.days) ? body.days : [];

  if (!deviceId || days.length === 0) {
    return json({ error: "device_id and a non-empty days[] are required" }, 400, cors);
  }

  const now = new Date().toISOString();
  const stmt = env.DB.prepare(
    `INSERT INTO stats
       (device_id, device_name, day, keystrokes, words, deletions, alt_tabs, power_cycles, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(device_id, day) DO UPDATE SET
       device_name  = excluded.device_name,
       keystrokes   = excluded.keystrokes,
       words        = excluded.words,
       deletions    = excluded.deletions,
       alt_tabs     = excluded.alt_tabs,
       power_cycles = excluded.power_cycles,
       updated_at   = excluded.updated_at`
  );

  const batch = days.slice(0, 5000).map((d) =>
    stmt.bind(
      deviceId,
      deviceName,
      String(d.day || "").slice(0, 10),
      clampInt(d.keystrokes),
      clampInt(d.words),
      clampInt(d.deletions),
      clampInt(d.alt_tabs),
      clampInt(d.power_cycles),
      now
    )
  );

  await env.DB.batch(batch);
  return json({ ok: true, rows: batch.length }, 200, cors);
}

async function handleTotals(env, cors) {
  const g = await env.DB.prepare(
    `SELECT
       COALESCE(SUM(keystrokes), 0)   AS keystrokes,
       COALESCE(SUM(words), 0)        AS words,
       COALESCE(SUM(deletions), 0)    AS deletions,
       COALESCE(SUM(alt_tabs), 0)     AS alt_tabs,
       COALESCE(SUM(power_cycles), 0) AS power_cycles,
       COUNT(DISTINCT device_id)      AS device_count
     FROM stats`
  ).first();

  const devices = await env.DB.prepare(
    `SELECT device_id, device_name,
       SUM(keystrokes)   AS keystrokes,
       SUM(words)        AS words,
       SUM(deletions)    AS deletions,
       SUM(alt_tabs)     AS alt_tabs,
       SUM(power_cycles) AS power_cycles,
       MAX(updated_at)   AS updated_at
     FROM stats
     GROUP BY device_id, device_name
     ORDER BY keystrokes DESC`
  ).all();

  return json(
    {
      global: {
        keystrokes: g.keystrokes,
        words: g.words,
        deletions: g.deletions,
        alt_tabs: g.alt_tabs,
        power_cycles: g.power_cycles,
      },
      device_count: g.device_count,
      devices: devices.results || [],
    },
    200,
    cors
  );
}
