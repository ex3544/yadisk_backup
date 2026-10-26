/**
 * Приём диагностических отчётов Yandex.Disk Backup и пересылка в Telegram.
 *
 * Маршруты (все требуют заголовок X-Report-Key):
 *   GET  /health  — проверка бота и чата
 *   POST /report  — multipart/form-data, поле file (ZIP)
 *   POST /message — {"text": "..."} для теста
 *
 * Секреты (wrangler secret put):
 *   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, REPORT_KEY
 */

const MAX_BYTES = 45 * 1024 * 1024; // Telegram: до 50 МБ на документ

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
