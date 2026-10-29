/**
 * Приём диагностических отчётов Yandex.Disk Backup и пересылка в Telegram.
 *
 * Маршруты (все требуют заголовок X-Report-Key):
 *   GET  /health    — проверка бота и чата
 *   POST /report    — multipart/form-data, поле file (ZIP)
 *   POST /message   — {"text": "..."} для теста
 *   POST /heartbeat — JSON-сводка от клиента; алерты уходят в Telegram
 *   GET  /summary   — сводка по всем установкам (нужна привязка KV)
 *
 * Секреты (wrangler secret put):
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, REPORT_KEY
 *
 * Хранилище (необязательно): KV-пространство HEARTBEATS. Без него heartbeat
 * принимается, алерты отправляются, но /summary пустой.
 */

const MAX_BYTES = 45 * 1024 * 1024; // Telegram: до 50 МБ на документ
const MAX_HEARTBEAT_BYTES = 64 * 1024;
const HEARTBEAT_TTL_SEC = 30 * 24 * 60 * 60;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!authorized(request, env.REPORT_KEY)) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }
    try {
      if (request.method === "GET" && url.pathname === "/health") {
        return await health(env);
      }
      if (request.method === "POST" && url.pathname === "/report") {
        return await report(request, env);
      }
      if (request.method === "POST" && url.pathname === "/message") {
        return await message(request, env);
      }
      if (request.method === "POST" && url.pathname === "/heartbeat") {
        return await heartbeat(request, env);
      }
      if (request.method === "GET" && url.pathname === "/summary") {
        return await summary(env);
      }
      return json({ ok: false, error: "not_found" }, 404);
    } catch (e) {
      console.error("worker error:", e && e.stack || e);
      return json({ ok: false, error: "internal_error" }, 500);
    }
  },
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function authorized(request, expected) {
  const got = request.headers.get("X-Report-Key") || "";
  if (!expected || got.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++) {
    diff |= got.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

async function tg(env, method, params) {
  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params || {}),
    },
  );
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    data = { ok: false, description: `telegram_http_${res.status}` };
  }
  return data;
}

async function health(env) {
  const me = await tg(env, "getMe");
  const chat = await tg(env, "getChat", { chat_id: env.TELEGRAM_CHAT_ID });
  const bot = me.ok
    ? {
        id: me.result.id,
        username: me.result.username,
        name: me.result.first_name,
      }
    : { error: me.description || "getMe failed" };
  const chatInfo = chat.ok
    ? {
        id: chat.result.id,
        type: chat.result.type,
        name: [chat.result.first_name, chat.result.last_name]
          .filter(Boolean)
          .join(" "),
        username: chat.result.username,
      }
    : { error: chat.description || "getChat failed" };
  return json({
    ok: !!(me.ok && chat.ok),
    bot,
    chat: chatInfo,
    time: new Date().toISOString(),
  });
}

async function report(request, env) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BYTES) {
    return json({ ok: false, error: "too_large" }, 413);
  }

  let form;
  try {
    form = await request.formData();
  } catch (_) {
    return json({ ok: false, error: "bad_form" }, 400);
  }

  const file = form.get("file");
  if (!file || typeof file === "string" || !("size" in file)) {
    return json({ ok: false, error: "file_required" }, 400);
  }
  if (file.size > MAX_BYTES) {
    return json({ ok: false, error: "too_large" }, 413);
  }

  const name = safeName(file.name);
  const appVersion = clip(form.get("app_version"), 32);
  const reportId = clip(form.get("report_id"), 64);
  const machine = clip(form.get("machine_id"), 64);

  const caption = [
    "Отчёт Yandex.Disk Backup",
    appVersion ? `версия ${appVersion}` : "",
    reportId ? `ID ${reportId}` : "",
    machine ? `машина ${machine}` : "",
    `${(file.size / 1024 / 1024).toFixed(2)} МБ`,
  ]
    .filter(Boolean)
    .join(" · ")
    .slice(0, 1024);

  const fd = new FormData();
  fd.append("chat_id", env.TELEGRAM_CHAT_ID);
  fd.append("caption", caption);
  fd.append("document", file, name);

  const res = await fetch(
    `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`,
    { method: "POST", body: fd },
  );
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    data = { ok: false, description: `telegram_http_${res.status}` };
  }
  if (!data.ok) {
    return json(
      { ok: false, error: data.description || `telegram_${res.status}` },
      502,
    );
  }
  return json({
    ok: true,
    message_id: data.result.message_id,
    file_name: name,
    size: file.size,
  });
}

async function message(request, env) {
  let body = {};
  try {
    body = await request.json();
  } catch (_) {
    body = {};
  }
  const text = clip(body && body.text, 4096) || "Тест связи: Yandex.Disk Backup";
  const data = await tg(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
  });
  if (!data.ok) {
    return json(
      { ok: false, error: data.description || "sendMessage failed" },
      502,
    );
  }
  return json({ ok: true, message_id: data.result.message_id });
}

/**
 * Приём heartbeat. Алерты (уже отдедуплицированные на клиенте) сразу уходят
 * в Telegram, сама сводка кладётся в KV для GET /summary.
 */
async function heartbeat(request, env) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_HEARTBEAT_BYTES) {
    return json({ ok: false, error: "too_large" }, 413);
  }
  let body = {};
  try {
    body = await request.json();
  } catch (_) {
    return json({ ok: false, error: "bad_json" }, 400);
  }
  const installId = clip(body.install_id, 64);
  if (!/^[0-9a-f]{12,32}$/i.test(installId)) {
    return json({ ok: false, error: "install_id_required" }, 400);
  }

  const record = {
    install_id: installId,
    app_version: clip(body.app_version, 32),
    received_at: new Date().toISOString(),
    phase: clip(body.phase, 32),
    disk: body.disk || {},
    unlim: body.unlim || {},
    free_gb: body.free_gb,
  };

  let stored = false;
  if (env.HEARTBEATS && typeof env.HEARTBEATS.put === "function") {
    try {
      await env.HEARTBEATS.put(`hb:${installId}`, JSON.stringify(record), {
        expirationTtl: HEARTBEAT_TTL_SEC,
      });
      stored = true;
    } catch (e) {
      console.error("kv put failed:", e && e.message);
    }
  }

  let sent = 0;
  const alerts = Array.isArray(body.alerts) ? body.alerts.slice(0, 10) : [];
  for (const alert of alerts) {
    const text = clip(alert && alert.text, 900);
    if (!text) continue;
    const line = `⚠ ${record.app_version || "?"} · ${installId}\n${text}`;
    const res = await tg(env, "sendMessage", {
      chat_id: env.TELEGRAM_CHAT_ID,
      text: line.slice(0, 4096),
    });
    if (res.ok) sent += 1;
  }

  return json({ ok: true, stored, alerts_sent: sent });
}

/** Сводка по всем установкам за последние 30 дней. */
async function summary(env) {
  if (!env.HEARTBEATS || typeof env.HEARTBEATS.list !== "function") {
    return json({ ok: false, error: "kv_not_bound" }, 501);
  }
  const list = await env.HEARTBEATS.list({ prefix: "hb:", limit: 1000 });
  const installs = [];
  const now = Date.now();
  let active24 = 0;
  let failed = 0;
  let downloaded = 0;
  for (const key of list.keys) {
    const raw = await env.HEARTBEATS.get(key.name);
    if (!raw) continue;
    let item;
    try {
      item = JSON.parse(raw);
    } catch (_) {
      continue;
    }
    const ageSec = (now - Date.parse(item.received_at || 0)) / 1000;
    if (ageSec < 24 * 3600) active24 += 1;
    failed += Number((item.disk && item.disk.failed) || 0)
      + Number((item.unlim && item.unlim.failed) || 0);
    downloaded += Number((item.disk && item.disk.downloaded) || 0)
      + Number((item.unlim && item.unlim.downloaded) || 0);
    installs.push({
      install_id: item.install_id,
      app_version: item.app_version,
      received_at: item.received_at,
      phase: item.phase,
      free_gb: item.free_gb,
    });
  }
  installs.sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at));
  return json({
    ok: true,
    total: installs.length,
    active_24h: active24,
    downloaded_files: downloaded,
    failed_files: failed,
    installs: installs.slice(0, 200),
  });
}

function clip(value, max) {
  if (value == null) return "";
  return String(value).replace(/[\r\n]+/g, " ").trim().slice(0, max);
}

function safeName(name) {
  let out = String(name || "report.zip")
    .replace(/[^\w.\-]+/g, "_")
    .slice(0, 100);
  if (!/\.zip$/i.test(out)) out += ".zip";
  return out;
}
