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
  'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
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
    return json({ ok: true, badge, group: newGroup });
  } catch (e) {
    return json({ error: e.message }, 500);
  }
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
