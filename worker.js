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
  const base64 = btoa(String.fromCharCode(...new TextEncoder().encode(text)));
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
    const base64 = btoa(String.fromCharCode(...bytes));
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
    const { name, folder, files = [] } = body;
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
      date: new Date().toISOString(),
      files: files.map(f => ({ name: f.name, type: (f.name || '').split('.').pop() })),
      status: 'pending',
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

    data.groups.push(newGroup);
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
    await writeData(data, sha, env);

    return json({ ok: true, deleted, removedBadge: badge });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}
