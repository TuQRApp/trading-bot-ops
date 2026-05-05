// trading-upload — Cloudflare Worker
// Deploy: paste this into the Cloudflare dashboard editor and Save & Deploy
// Secrets required: GITHUB_TOKEN (set in Worker > Settings > Variables > Secrets)

const REPO      = 'TuQRApp/trading-bot-ops';
const BRANCH    = 'main';
const ROOT      = 'Archivos';
const DATA_PATH = 'data.json';

// Color palette for new groups (cycles through these fsh classes)
const FSH_PALETTE = ['fsh-a','fsh-b','fsh-c','fsh-d','fsh-e','fsh-f','fsh-g','fsh-h'];

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, 'Content-Type': 'application/json' },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') return new Response(null, { headers: CORS });
    const { pathname } = new URL(request.url);
    const method = request.method;

    if (pathname === '/upload'  && method === 'POST')   return handleUpload(request, env);
    if (pathname === '/folders' && method === 'GET')    return handleFolders(env);
    if (pathname === '/files'   && method === 'GET')    return handleFiles(request, env);
    if (pathname === '/data'    && method === 'GET')    return handleGetData(env);
    if (pathname === '/data'    && method === 'PUT')    return handlePutData(request, env);
    if (pathname === '/group'   && method === 'POST')   return handleRegisterGroup(request, env);
    if (pathname === '/group'   && method === 'PATCH')  return handlePatchGroup(request, env);
    if (pathname === '/group'   && method === 'DELETE') return handleDeleteGroup(request, env);
    if (pathname === '/folder'  && method === 'POST')   return handleCreateFolder(request, env);
    if (pathname === '/folder'  && method === 'DELETE') return handleDeleteFolder(request, env);
    if (pathname === '/dispatch-m5'   && method === 'POST') return handleDispatchM5(request, env);
    if (pathname === '/status'        && method === 'GET')  return handleGetStatus(env);
    if (pathname === '/chat'          && method === 'POST') return handleChat(request, env);
    if (pathname === '/generate-spec' && method === 'POST') return handleGenerateSpec(request, env);

    return new Response('Not found', { status: 404, headers: CORS });
  },
};

// ── GitHub API helpers ───────────────────────────────────────────────────────

function ghHeaders(env) {
  return {
    'Authorization': 'Bearer ' + env.GITHUB_TOKEN,
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'trading-worker',
    'X-GitHub-Api-Version': '2022-11-28',
  };
}

async function ghGet(path, env) {
  return fetch(
    'https://api.github.com/repos/' + REPO + '/contents/' + path + '?ref=' + BRANCH,
    { headers: ghHeaders(env) }
  );
}

async function ghPut(path, base64Content, sha, commitMsg, env) {
  const body = {
    message: commitMsg || 'chore: update ' + path,
    content: base64Content,
    branch: BRANCH,
  };
  if (sha) body.sha = sha;
  return fetch(
    'https://api.github.com/repos/' + REPO + '/contents/' + path,
    {
      method: 'PUT',
      headers: { ...ghHeaders(env), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }
  );
}

async function ghDelete(path, sha, env) {
  return fetch(
    'https://api.github.com/repos/' + REPO + '/contents/' + path,
    {
      method: 'DELETE',
      headers: { ...ghHeaders(env), 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'chore: delete ' + path, sha, branch: BRANCH }),
    }
  );
}

// ── data.json read/write ─────────────────────────────────────────────────────

async function readData(env) {
  const r = await ghGet(DATA_PATH, env);
  if (!r.ok) throw new Error('Cannot read data.json: HTTP ' + r.status);
  const info = await r.json();
  const text = new TextDecoder().decode(
    Uint8Array.from(atob(info.content.replace(/\n/g, '')), c => c.charCodeAt(0))
  );
  return { data: JSON.parse(text), sha: info.sha };
}

async function writeData(data, sha, env) {
  const text = JSON.stringify(data, null, 2);
  const bytes = new TextEncoder().encode(text);
  // Loop-based base64 avoids call stack overflow on large data.json files
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  const base64 = btoa(binary);
  const r = await ghPut(DATA_PATH, base64, sha, 'chore: update data.json', env);
  if (!r.ok) {
    const err = await r.text();
    throw new Error('Cannot write data.json: HTTP ' + r.status + ' — ' + err);
  }
}

// ── /upload  POST ─────────────────────────────────────────────────────────────
// FormData fields: file (Blob), folder (string)

async function handleUpload(request, env) {
  try {
    const form = await request.formData();
    const file = form.get('file');
    const folder = (form.get('folder') || '').trim();
    if (!file || !folder) return json({ error: 'file and folder are required' }, 400);

    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.length > 10 * 1024 * 1024) return json({ error: 'Archivo demasiado grande (máx 10 MB). Los archivos HTML de resultados no necesitan subirse — solo sube el .py y los .csv.' }, 413);
    let base64 = '';
    const CHUNK = 8192;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      base64 += btoa(String.fromCharCode(...bytes.subarray(i, i + CHUNK)));
    }
    const path = ROOT + '/' + folder + '/' + file.name;

    // Check if file already exists (need SHA to update)
    const existing = await ghGet(path, env);
    const sha = existing.ok ? (await existing.json()).sha : undefined;

    const r = await ghPut(path, base64, sha, 'feat: upload ' + file.name, env);
    if (!r.ok) return json({ error: await r.text() }, r.status);
    return json({ ok: true, path });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /folders  GET ─────────────────────────────────────────────────────────────
// Returns list of existing subfolder names under Archivos/

async function handleFolders(env) {
  try {
    const r = await fetch(
      'https://api.github.com/repos/' + REPO + '/git/trees/' + BRANCH + '?recursive=1',
      { headers: ghHeaders(env) }
    );
    if (!r.ok) return json({ folders: [] });
    const tree = await r.json();
    const prefix = ROOT + '/';
    const folders = new Set();
    for (const item of tree.tree || []) {
      if (item.type === 'tree' && item.path.startsWith(prefix)) {
        const rest = item.path.slice(prefix.length);
        if (!rest.includes('/')) folders.add(rest); // only direct subfolders
      }
    }
    return json({ folders: [...folders].sort() });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /files  GET ──────────────────────────────────────────────────────────────
// ?folder=Canal+Fibonacci  →  lists files in Archivos/{folder}/

async function handleFiles(request, env) {
  try {
    const folder = new URL(request.url).searchParams.get('folder');
    if (!folder) return json({ error: 'folder param required' }, 400);
    const r = await ghGet(ROOT + '/' + folder, env);
    if (!r.ok) return json({ files: [] });
    const items = await r.json();
    return json({
      files: Array.isArray(items)
        ? items.map(f => ({ name: f.name, size: f.size, type: f.name.split('.').pop() }))
        : [],
    });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /data  GET ───────────────────────────────────────────────────────────────

async function handleGetData(env) {
  try {
    const { data } = await readData(env);
    return json(data);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /data  PUT ───────────────────────────────────────────────────────────────
// Body: full data.json object

async function handlePutData(request, env) {
  try {
    const newData = await request.json();
    const { sha } = await readData(env);
    await writeData(newData, sha, env);
    return json({ ok: true });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── Resend email notification ────────────────────────────────────────────────

async function sendUploadEmail(group, env) {
  if (!env.RESEND_API_KEY) return;
  const fileList = group.files.map(f => `<li style="font-family:monospace">${f.name}</li>`).join('');
  const html = `
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto">
      <h2 style="color:#1e2d6e;margin-bottom:4px">Nuevo archivo subido</h2>
      <p style="color:#64748b;margin-top:0">${group.badge} · ${new Date(group.date).toISOString().slice(0,16).replace('T',' ')} UTC</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0">
        <tr><td style="color:#64748b;width:110px;padding:6px 0">Nombre</td><td style="font-weight:700">${group.name}</td></tr>
        <tr><td style="color:#64748b;padding:6px 0">Carpeta</td><td style="font-family:monospace">${group.folder}</td></tr>
        <tr><td style="color:#64748b;padding:6px 0">Archivos</td><td><ul style="margin:0;padding-left:18px">${fileList}</ul></td></tr>
      </table>
      <a href="https://tuqrapp.github.io/trading-bot-ops/" style="display:inline-block;background:#1e2d6e;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:14px">Ver dashboard →</a>
    </div>`;
  await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + env.RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from: 'Trading Bot <onboarding@resend.dev>',
      to: ['nestragues@icloud.com'],
      subject: '[Trading Bot] ' + group.badge + ' — ' + group.name,
      html,
    }),
  });
}

// ── /group  POST ─────────────────────────────────────────────────────────────
// Registers a new group in data.json after files have been uploaded
// Body: { name: string, folder: string, files: [{name, type}] }

async function handleRegisterGroup(request, env) {
  try {
    const body = await request.json();
    const { name, folder, files = [], folder_id = null, version_of = null } = body;
    if (!name || !folder) return json({ error: 'name and folder are required' }, 400);

    const { data, sha } = await readData(env);

    // Badge: next FILE-NNN
    const n = data.groups.length + 1;
    const badge = 'FILE-' + String(n).padStart(3, '0');

    // fsh_class from palette (cycle)
    const fshClass = FSH_PALETTE[data.groups.length % FSH_PALETTE.length];

    // Dominant file type from uploaded files
    const ftypes = files.map(f => (f.name || '').split('.').pop().toLowerCase());
    const ftype = ftypes[0] || 'txt';
    const iconMap = { py:'🐍', csv:'📊', json:'📋', js:'💻', ts:'💻', html:'🌐', log:'📜', png:'🖼', txt:'📄' };
    const icon = iconMap[ftype] || '📁';

    const newGroup = {
      id: fshClass,
      fsh_class: fshClass,
      name,
      badge,
      icon,
      ftype,
      folder,
      folder_id: folder_id || null,
      date: new Date().toISOString(),
      files: files.map(f => ({ name: f.name, type: (f.name || '').split('.').pop() })),
      status: 'pending',
      version_of: version_of || null,
      m1: {
        type: 'empty',
        last_updated: null,
        empty_title: name + ' — análisis pendiente',
        empty_desc: 'Archivos subidos correctamente. El análisis comenzará en la próxima sesión de trabajo con el bot.',
        empty_trigger: files.map(f => f.name).join(' · '),
      },
      m2: [],
      m3: [],
      m4: [],
    };
    if (!version_of) newGroup.versions = [badge];

    data.groups.push(newGroup);

    if (version_of) {
      const parentIdx = data.groups.findIndex(g => g.badge === version_of);
      if (parentIdx !== -1) {
        if (!data.groups[parentIdx].versions) data.groups[parentIdx].versions = [version_of];
        data.groups[parentIdx].versions.push(badge);
      }
    }

    await writeData(data, sha, env);
    sendUploadEmail(newGroup, env).catch(() => {}); // fire-and-forget
    return json({ ok: true, badge, group: newGroup });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /group  PATCH ────────────────────────────────────────────────────────────
// Actions: submit_review | request_rereview
// submit_review body: { badge, action, corrections:{cardId:text}, trader_notes, rereview_notes? }
// request_rereview body: { badge, action, rereview_notes }

async function handlePatchGroup(request, env) {
  try {
    const body = await request.json();
    const { badge, action } = body;
    if (!badge || !action) return json({ error: 'badge and action required' }, 400);

    const { data, sha } = await readData(env);
    const idx = data.groups.findIndex(g => g.badge === badge);
    if (idx === -1) return json({ error: 'group not found' }, 404);
    const group = data.groups[idx];

    if (action === 'move') {
      group.folder_id = body.folder_id || null;
      await writeData(data, sha, env);
      return json({ ok: true });
    }

    if (action === 'save_m5_notes') {
      if (!group.m5) group.m5 = {};
      group.m5.trader_notes = body.notes || '';
      await writeData(data, sha, env);
      return json({ ok: true });
    }

    if (action === 'save_card_field') {
      const { cardId, field, value } = body;
      if (!cardId || !field) return json({ error: 'cardId and field required' }, 400);
      let found = false;
      for (const mod of ['m2', 'm3', 'm4']) {
        if (!Array.isArray(group[mod])) continue;
        const card = group[mod].find(c => c.id === cardId);
        if (card) { card[field] = value || ''; found = true; break; }
      }
      if (!found && (cardId === 'M1-general' || cardId === 'M1-quality' || cardId === 'M1-readiness')) {
        if (!group.m1) group.m1 = {};
        group.m1[field] = value || '';
        found = true;
      }
      if (!found && cardId.startsWith('m5-card-')) {
        const idx = parseInt(cardId.replace('m5-card-', ''), 10);
        if (group.m5 && Array.isArray(group.m5.cards) && group.m5.cards[idx]) {
          group.m5.cards[idx][field] = value || '';
          found = true;
        }
      }
      if (!found) return json({ error: 'card not found' }, 404);
      await writeData(data, sha, env);
      return json({ ok: true });
    }

    if (action === 'submit_review') {
      const corrections = body.corrections || {};
      for (const mod of ['m2', 'm3', 'm4']) {
        if (!Array.isArray(group[mod])) continue;
        for (const card of group[mod]) {
          if (corrections[card.id] !== undefined) card.correction = corrections[card.id];
        }
      }
      // Save M1 correction (card id may be M1-general, M1-quality, or M1-readiness)
      if (!group.m1) group.m1 = {};
      const m1Correction = corrections['M1-general'] || corrections['M1-quality'] || corrections['M1-readiness'] || '';
      if (m1Correction) group.m1.correction = m1Correction;
      group.trader_notes = body.trader_notes || '';
      group.revision_submitted = true;
      group.status = 'pendiente_final';
      await writeData(data, sha, env);
      sendReviewSubmittedEmail(group, env).catch(() => {});
      return json({ ok: true });
    }

    if (action === 'request_rereview') {
      group.status = 'en_revision';
      group.rereview_requested = true;
      group.revision_submitted = false;
      group.rereview_notes = body.rereview_notes || '';
      await writeData(data, sha, env);
      return json({ ok: true });
    }

    return json({ error: 'unknown action' }, 400);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

async function sendReviewSubmittedEmail(group, env) {
  if (!env.RESEND_API_KEY) return;
  const html = `
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto">
      <h2 style="color:#1e2d6e;margin-bottom:4px">Revisión lista para finalizar</h2>
      <p style="color:#64748b;margin-top:0">${group.badge} · ${group.name}</p>
      <p style="font-size:14px;color:#1a2757">El trader revisó el borrador y envió sus correcciones.<br>Abrí Claude Code en el directorio Trading para finalizar el análisis.</p>
      <p style="font-family:monospace;font-size:12px;color:#64748b;background:#f1f5f9;padding:10px 14px;border-radius:6px">status: pendiente_final → abrir Claude Code</p>
    </div>`;
  await fetch('https://api.resend.com/emails', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + env.RESEND_API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from: 'Trading Bot <onboarding@resend.dev>',
      to: ['nestragues@gmail.com'],
      subject: '[Trading Bot] Revisión lista — ' + group.badge + ' — ' + group.name,
      html,
    }),
  });
}

// ── /group  DELETE ───────────────────────────────────────────────────────────
// Body: { badge: "FILE-002" }
// Deletes all files from GitHub (if they exist) and removes group from data.json

async function handleDeleteGroup(request, env) {
  try {
    const body = await request.json();
    const { badge } = body;
    if (!badge) return json({ error: 'badge is required' }, 400);

    const { data, sha } = await readData(env);
    const idx = data.groups.findIndex(g => g.badge === badge);
    if (idx === -1) return json({ error: 'group not found' }, 404);

    const group = data.groups[idx];
    const deleted = [];

    // Delete files from GitHub (best-effort, ignore missing)
    if (group.folder && group.files && group.files.length > 0) {
      for (const f of group.files) {
        const path = ROOT + '/' + group.folder + '/' + f.name;
        try {
          const fr = await ghGet(path, env);
          if (fr.ok) {
            const info = await fr.json();
            const dr = await ghDelete(path, info.sha, env);
            if (dr.ok) deleted.push(path);
          }
        } catch (_) {
          // file doesn't exist or error — skip
        }
      }
    }

    // Remove group from data.json
    data.groups.splice(idx, 1);
    // Remove badge from any parent group's versions array
    for (const pg of data.groups) {
      if (Array.isArray(pg.versions) && pg.versions.includes(badge)) {
        pg.versions = pg.versions.filter(b => b !== badge);
      }
    }
    await writeData(data, sha, env);

    return json({ ok: true, deleted, removedBadge: badge });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /folder  POST ─────────────────────────────────────────────────────────────

async function handleCreateFolder(request, env) {
  try {
    const body = await request.json();
    const name = (body.name || '').trim();
    if (!name) return json({ error: 'name is required' }, 400);
    const { data, sha } = await readData(env);
    if (!Array.isArray(data.folders)) data.folders = [];
    const id = 'f' + Date.now();
    data.folders.push({ id, name, parent_id: body.parent_id || null });
    await writeData(data, sha, env);
    return json({ ok: true, id });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /folder  DELETE ───────────────────────────────────────────────────────────

async function handleDeleteFolder(request, env) {
  try {
    const body = await request.json();
    const { id } = body;
    if (!id) return json({ error: 'id is required' }, 400);
    const { data, sha } = await readData(env);
    if (!Array.isArray(data.folders)) data.folders = [];
    const folder = data.folders.find(f => f.id === id);
    const parentId = folder ? folder.parent_id : null;
    // Re-parent children and orphaned groups to this folder's parent
    data.folders.forEach(f => { if (f.parent_id === id) f.parent_id = parentId; });
    (data.groups || []).forEach(g => { if (g.folder_id === id) g.folder_id = parentId; });
    data.folders = data.folders.filter(f => f.id !== id);
    await writeData(data, sha, env);
    return json({ ok: true });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /status  GET ─────────────────────────────────────────────────────────────
// Returns lightweight group metadata — badge, name, status, date — without m1-m4 payload.
// Claude Code sessions use this at startup to decide whether a full data.json read is needed.

async function handleGetStatus(env) {
  try {
    const { data } = await readData(env);
    const statuses = (data.groups || []).map(g => ({
      badge: g.badge,
      name: g.name,
      status: g.status,
      date: g.date,
    }));
    return json(statuses);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /dispatch-m5  POST ────────────────────────────────────────────────────────
// Triggers the market-context GitHub Actions workflow manually.

async function handleDispatchM5(request, env) {
  try {
    const r = await fetch(
      'https://api.github.com/repos/' + REPO + '/actions/workflows/market-context.yml/dispatches',
      {
        method: 'POST',
        headers: { ...ghHeaders(env), 'Content-Type': 'application/json' },
        body: JSON.stringify({ ref: BRANCH }),
      }
    );
    if (r.ok || r.status === 204) return json({ ok: true });
    const err = await r.text();
    return json({ error: err }, 500);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /chat  POST — streaming proxy a Claude API ───────────────────────────────
// Body   : { messages, context }
// Context: { bots_activos, market_context, trader_profile, manual, spec_actual }
// Response: SSE text/event-stream (Claude format, el cliente parsea delta.text)

async function handleChat(request, env) {
  if (!env.ANTHROPIC_API_KEY)
    return json({ error: 'ANTHROPIC_API_KEY no configurado en el Worker' }, 500);
  try {
    const body = await request.json();
    const { messages = [], context = {} } = body;
    const system = buildChatSystem(context);
    const res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key'        : env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type'     : 'application/json',
      },
      body: JSON.stringify({
        model     : 'claude-sonnet-4-6',
        max_tokens: 1024,
        system,
        messages  : normalizeMessages(messages),
        stream    : true,
      }),
    });
    if (!res.ok) {
      const err = await res.text();
      return json({ error: 'Claude API: ' + err }, res.status);
    }
    return new Response(res.body, {
      headers: {
        ...CORS,
        'Content-Type' : 'text/event-stream',
        'Cache-Control': 'no-cache',
      },
    });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── /generate-spec  POST — un pass de generacion de spec ────────────────────
// Body: { pass, spec, context, chat_history, generated_spec? }
// pass: 'pass1' (Claude genera) | 'pass2' (Claude critica) | 'pass3' (GPT-4o MT5)

async function handleGenerateSpec(request, env) {
  try {
    const body = await request.json();
    const { pass, spec = {}, context = {}, generated_spec = null } = body;

    if (pass === 'pass1') {
      if (!env.ANTHROPIC_API_KEY)
        return json({ error: 'ANTHROPIC_API_KEY no configurado' }, 500);
      const system  = buildPass1System(context);
      const userMsg = buildPass1User(spec, context);
      const result  = await claudeJsonCall(system, userMsg, env, 4096);
      return json(result);
    }

    if (pass === 'pass2') {
      if (!env.ANTHROPIC_API_KEY)
        return json({ error: 'ANTHROPIC_API_KEY no configurado' }, 500);
      const system  = buildPass2System(context);
      const userMsg = buildPass2User(generated_spec || spec);
      const result  = await claudeJsonCall(system, userMsg, env, 2048);
      return json(result);
    }

    if (pass === 'pass3') {
      if (!env.OPENAI_API_KEY)
        return json({ error: 'OPENAI_API_KEY no configurado' }, 500);
      const system  = buildPass3System();
      const userMsg = buildPass3User(generated_spec || spec);
      const result  = await gptJsonCall(system, userMsg, env);
      return json(result);
    }

    return json({ error: 'pass invalido — usar pass1, pass2 o pass3' }, 400);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

// ── Context builders ─────────────────────────────────────────────────────────

function buildBotsText(bots) {
  if (!Array.isArray(bots) || !bots.length) return 'Sin bots activos.';
  return bots.map(g => {
    const m1      = g.m1 || {};
    const metrics = Array.isArray(m1.metrics) ? m1.metrics : [];
    const wr = (metrics.find(m => m.label && /win/i.test(m.label))?.value) || '—';
    const pf = (metrics.find(m => m.label && /profit/i.test(m.label))?.value) || '—';
    return `${g.badge} — ${g.name}\n  ${g.category || ''}\n  WR: ${wr} | PF: ${pf}\n  ${g.summary || ''}`;
  }).join('\n\n');
}

function buildM5Text(mc) {
  if (!mc) return 'Sin contexto de mercado disponible.';
  const lines = [`Fecha: ${mc.date || '?'}`];
  if (mc.vix?.value)                lines.push(`VIX: ${mc.vix.value} (${mc.vix.regime})`);
  if (mc.fear_greed_crypto?.value)  lines.push(`Fear & Greed crypto: ${mc.fear_greed_crypto.value} — ${mc.fear_greed_crypto.label}`);
  if (mc.binance?.funding_bias)     lines.push(`BTC funding: ${mc.binance.funding_bias}`);
  const cards = (mc.bots_context || mc.cards || []).slice(0, 6);
  cards.forEach(c => lines.push(`[${c.tipo || c.type || 'info'}] ${c.title}: ${c.desc}`));
  return lines.join('\n');
}

function buildProfileText(tp) {
  if (!tp || (tp.cycles || 0) < 2) return 'Perfil sin ciclos suficientes (menos de 2 ciclos completados).';
  const lines = [`Ciclos completados: ${tp.cycles}`];
  if (tp.tipos) {
    Object.entries(tp.tipos).forEach(([tipo, rates]) => {
      const pct = Math.round((rates.descartado || 0) * 100);
      lines.push(`  ${tipo}: ${pct}% descarte historico`);
    });
  }
  return lines.join('\n');
}

function buildSpecText(spec) {
  if (!spec) return 'Sin spec definida.';
  const labels = {
    'instrumentos': 'Instrumento(s)',
    'sesion'      : 'Sesion operativa',
    'm5'          : 'Influencia M5',
    'duracion'    : 'Duracion',
    'tf-entrada'  : 'TF de entrada',
    'tf-apoyo'    : 'TFs de apoyo',
    'senal'       : 'Tipo de senal',
    'ejecucion'   : 'Tipo de ejecucion',
    'tp'          : 'Toma de ganancias',
    'sl'          : 'Stop Loss',
    'timeout'     : 'Timeout',
    'riesgo'      : 'Riesgo por trade (%)',
    'maxpos'      : 'Posiciones max',
    'cb'          : 'Circuit Breaker',
  };
  return Object.entries(spec)
    .map(([k, v]) => `${labels[k] || k}: ${v}`)
    .join('\n');
}

function buildManualCtxText(manual = []) {
  return manual.map(m => {
    if (m.type === 'image') return `[Imagen de referencia adjunta: ${m.name}]`;
    if (m.type === 'note')  return `[Nota del trader]: ${m.content || ''}`;
    const content = (m.content || '').slice(0, 6000);
    return `[Archivo: ${m.name}]\n${content}`;
  }).join('\n\n---\n\n');
}

// ── System prompts ───────────────────────────────────────────────────────────

function buildChatSystem(context) {
  const botsText    = buildBotsText(context.bots_activos);
  const m5Text      = buildM5Text(context.market_context);
  const profileText = buildProfileText(context.trader_profile);
  const manualText  = buildManualCtxText(context.manual || []);
  const specText    = buildSpecText(context.spec_actual);

  return `Sos un asistente experto en diseno de bots de trading para IC Markets MT5 en Python.
Tu rol es guiar al trader para definir las 14 dimensiones de su nueva estrategia.
Sos conciso y practico. Una o dos preguntas por turno, no mas.

=== BOTS YA ACTIVOS ===
${botsText}

=== CONTEXTO DE MERCADO HOY (M5) ===
${m5Text}

=== PERFIL DEL TRADER ===
${profileText}

=== ESTADO ACTUAL DE LA SPEC (lo que ya definio el trader) ===
${specText}

${manualText ? '=== CONTEXTO ADICIONAL ADJUNTO ===\n' + manualText : ''}

=== REGLAS INVIOLABLES ===
- Nunca sugerir scalping (< 5 min): el spread de IC Markets Raw elimina el margen
- Nunca sugerir dependencias de datos externos en tiempo real durante el loop del bot
- Nunca sugerir senales que requieran precision sub-segundo (loop MT5 Python es 20-30s)
- Si el instrumento + TF + logica de entrada se superpone con un bot activo, decirlo explicitamente
- El SL debe ser al menos 3x el spread tipico del instrumento
- Plataforma objetivo: Python + MetaTrader5 lib, loop poll, cuenta real IC Markets Raw
- Para las dimensiones con valor PENDIENTE: guiar al trader para definirlas
- Para las dimensiones con valor NO_CONSIDERAR: no volver a preguntar por ellas`;
}

function buildPass1System(context) {
  const botsText = buildBotsText(context.bots_activos);
  const m5Text   = buildM5Text(context.market_context);

  return `Sos un quant analyst senior especializado en bots de trading para IC Markets MT5 Python.
Tu tarea: generar una spec completa y realista a partir de las dimensiones definidas por el trader.

BOTS ACTIVOS (no duplicar sin valor diferencial claro):
${botsText}

MERCADO HOY:
${m5Text}

REGLAS:
- Para dimensiones PENDIENTE: proponé el valor mas razonable dado el contexto del trader
- Para dimensiones NO_CONSIDERAR: omitilas de la spec final (no incluirlas)
- No incluyas scalping ni dependencias de APIs externas en tiempo real
- El SL debe ser >= 3x el spread tipico del instrumento
- Todo implementable en MT5 Python con loop de 20-30 segundos

Responde SOLO con JSON valido sin markdown ni texto adicional:
{
  "name": "nombre corto del bot (maximo 40 caracteres)",
  "summary": "2-3 oraciones densas: que hace, como, diferencial respecto a bots activos",
  "spec": {
    "instrumentos"   : "...",
    "sesion"         : "...",
    "m5_rol"         : "...",
    "duracion"       : "...",
    "tf_entrada"     : "...",
    "tf_apoyo"       : "...",
    "senal"          : "...",
    "ejecucion"      : "...",
    "tp"             : "...",
    "sl"             : "...",
    "timeout"        : "...",
    "riesgo_pct"     : "...",
    "max_posiciones" : "...",
    "circuit_breaker": "..."
  },
  "warnings": [],
  "critical": []
}`;
}

function buildPass1User(spec, context) {
  const manualText = buildManualCtxText(context.manual || []);
  return `Dimensiones definidas por el trader:\n${buildSpecText(spec)}${manualText ? '\n\nContexto adicional:\n' + manualText : ''}\n\nGenera la spec completa en JSON.`;
}

function buildPass2System(context) {
  const botsText = buildBotsText(context.bots_activos);
  return `Sos un QA auditor de estrategias de trading para IC Markets MT5.
Auditas una spec generada por un analista y buscas problemas reales.

BOTS ACTIVOS DEL TRADER:
${botsText}

COSTOS REALES IC MARKETS RAW (referencia):
- XAUUSD  : spread ~$0.12/pip + comision $3.50/lote/lado
- XAGUSD  : spread ~$0.02/pip + comision $3.50/lote/lado
- BTCUSD  : spread ~$15 + comision $5.00/lote/lado
- Forex major: spread 0.1-0.3 pips + comision $3.50/lote/lado
- Indices : spread 0.5-2 puntos segun instrumento

AUDITORIA — cheques obligatorios:
1. Duplicacion: mismo instrumento + TF similar + logica de entrada similar a un bot activo
2. R:R no viable: SL menor a 3x spread, o TP imposible de alcanzar antes del timeout
3. Senal irrealizable: requiere datos no disponibles en MT5 o precision sub-segundo
4. Circuit breaker ausente o mal calibrado para el perfil de riesgo
5. Costo total (spread + comision RT) que comprometa la rentabilidad con el sizing propuesto

Responde SOLO con JSON valido sin markdown:
{
  "warnings": ["advertencias — aspectos a revisar pero no bloqueantes"],
  "critical": ["problemas graves que deben resolverse antes de codificar el bot"]
}`;
}

function buildPass2User(generatedSpec) {
  return `Spec a auditar:\n${JSON.stringify(generatedSpec, null, 2)}`;
}

function buildPass3System() {
  return `You are a Python engineer specialized in MetaTrader 5 API development.
Review a trading bot strategy spec for MT5 Python implementation feasibility.

MT5 Python constraints:
- OHLCV data via copy_rates_from_pos / copy_rates_range
- Polling loop: typically 20-30 seconds per cycle
- Order types: BUY/SELL (market), BUY_LIMIT/SELL_LIMIT/BUY_STOP/SELL_STOP (pending)
- No native order book, no tick-by-tick streaming in Python
- order_calc_profit for P&L estimation, order_calc_margin for margin check
- history_deals_get for trade history, positions_get for open positions
- Magic number to identify bot orders

Check these specific points:
1. Are all required indicators implementable with pandas/numpy/ta-lib in the polling loop?
2. Can the entry signal be detected reliably within a 20-30 second polling interval?
3. Any known MT5 API race conditions or timing issues with this signal/order approach?
4. Is the order placement type (limit/market) appropriate for this signal latency?
5. Any complexity that significantly increases implementation bug risk?

Respond ONLY with valid JSON, no markdown, no text outside JSON:
{
  "mt5_notes": ["specific MT5 implementation notes and recommendations"],
  "warnings" : ["implementation concerns that may cause bugs"],
  "feasible" : true
}`;
}

function buildPass3User(generatedSpec) {
  return `Strategy spec to review for MT5 implementation:\n${JSON.stringify(generatedSpec, null, 2)}`;
}

// ── Claude non-streaming call — espera JSON en la respuesta ─────────────────

async function claudeJsonCall(system, userMsg, env, maxTokens = 2048) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key'        : env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'content-type'     : 'application/json',
    },
    body: JSON.stringify({
      model     : 'claude-sonnet-4-6',
      max_tokens: maxTokens,
      system,
      messages  : [{ role: 'user', content: userMsg }],
    }),
  });
  if (!res.ok) throw new Error('Claude API HTTP ' + res.status + ': ' + await res.text());
  const resp = await res.json();
  const text = resp.content?.[0]?.text || '';
  try {
    const clean = text.replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
    return JSON.parse(clean);
  } catch (e) {
    return { raw_response: text, parse_error: e.message };
  }
}

// ── GPT-4o non-streaming call — usa response_format: json_object ─────────────

async function gptJsonCall(system, userMsg, env) {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + env.OPENAI_API_KEY,
      'Content-Type' : 'application/json',
    },
    body: JSON.stringify({
      model          : 'gpt-4o',
      response_format: { type: 'json_object' },
      messages: [
        { role: 'system', content: system },
        { role: 'user',   content: userMsg },
      ],
    }),
  });
  if (!res.ok) throw new Error('OpenAI API HTTP ' + res.status + ': ' + await res.text());
  const resp = await res.json();
  const text = resp.choices?.[0]?.message?.content || '{}';
  try {
    return JSON.parse(text);
  } catch (e) {
    return { raw_response: text, parse_error: e.message };
  }
}

// ── normalizeMessages ────────────────────────────────────────────────────────
// Convierte el historial de chat al formato Claude API (alternating user/assistant).

function normalizeMessages(messages) {
  if (!Array.isArray(messages) || !messages.length) return [];

  const normalized = messages.map(m => ({
    role: m.role === 'assistant' ? 'assistant' : 'user',
    content: typeof m.content === 'string'
      ? [{ type: 'text', text: m.content }]
      : Array.isArray(m.content)
        ? m.content
        : [{ type: 'text', text: String(m.content) }],
  }));

  // Claude requiere que el primer mensaje sea del user
  while (normalized.length && normalized[0].role !== 'user') normalized.shift();

  // Fusionar mensajes consecutivos del mismo rol
  const merged = [];
  for (const msg of normalized) {
    if (merged.length && merged[merged.length - 1].role === msg.role) {
      merged[merged.length - 1].content.push({ type: 'text', text: '\n' }, ...msg.content);
    } else {
      merged.push({ role: msg.role, content: [...msg.content] });
    }
  }

  return merged;
}
