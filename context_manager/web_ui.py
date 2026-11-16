"""Local browser interface for project notes and agent memory."""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import Store
from .vault import ProjectVault


_PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CWM · Notes and memory</title>
  <style nonce="{{NONCE}}">
    :root{color-scheme:light dark;--bg:#f4f5f7;--panel:#fff;--text:#1d2430;--muted:#697586;--line:#dce1e8;--accent:#5267d8;--soft:#eef0ff;--danger:#a13434;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}

    @media(prefers-color-scheme:dark){:root{--bg:#17191e;--panel:#20232a;--text:#e7e9ef;--muted:#a5adba;--line:#383d47;--accent:#9aa8ff;--soft:#2b3049;--danger:#ff9696}
    }

    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text)}
    button,input,textarea,select{font:inherit;color:inherit}
    button{cursor:pointer;border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:7px 11px}
    button:hover{border-color:var(--accent)}
    button:disabled{opacity:.45;cursor:default}
    .primary{background:var(--accent);border-color:var(--accent);color:#fff}
    .quiet{background:transparent}
    .shell{height:100vh;min-height:580px;display:grid;grid-template-rows:auto 1fr}
    .top{display:flex;align-items:center;gap:14px;padding:11px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
    .brand{font-size:19px;font-weight:750}
    .sub{color:var(--muted);font-size:13px}
    .top .spacer{flex:1}
    .workspace{min-height:0;display:grid;grid-template-columns:255px minmax(300px,1fr) 310px;gap:1px;background:var(--line)}
    aside,main{min-width:0;min-height:0;background:var(--panel)}
    .nav,.right{overflow:auto;padding:16px}
    .nav{display:flex;flex-direction:column;gap:12px}
    .navhead{display:flex;align-items:center;justify-content:space-between}
    h1,h2,h3,p{margin-top:0}
    h1{font-size:22px;margin-bottom:4px}
    h2{font-size:15px;margin-bottom:9px}
    h3{font-size:14px;margin:12px 0 6px}
    .muted{color:var(--muted)}
    .search,input[type=text],textarea,select{border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:9px 10px;width:100%}
    .note-list{overflow:auto;display:flex;flex-direction:column;gap:4px}
    .note-item{border:0;text-align:left;background:transparent;padding:9px;border-radius:8px}
    .note-item.active{background:var(--soft);color:var(--text)}
    .note-item strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .note-item small{color:var(--muted)}
    main{display:flex;flex-direction:column}
    .editor-head{padding:22px 26px 12px;display:flex;gap:12px;align-items:flex-start;border-bottom:1px solid var(--line)}
    .editor-head .titles{min-width:0;flex:1}
    .editor-head h1{overflow-wrap:anywhere}
    .toolbar{display:flex;gap:8px}
    .tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .tag{font-size:12px;color:var(--muted);background:var(--bg);padding:2px 8px;border-radius:12px}
    .editor{padding:18px 26px;display:flex;flex-direction:column;min-height:0;flex:1}
    .editor textarea{flex:1;resize:none;min-height:250px;font:14px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;padding:14px}
    .preview{overflow:auto;flex:1;min-height:250px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:14px}
    .markdown{line-height:1.7;overflow-wrap:anywhere}
    .markdown h1,.markdown h2,.markdown h3,.markdown h4{margin:1em 0 .45em}
    .markdown p{margin:0 0 1em}
    .markdown pre{white-space:pre-wrap;overflow:auto;background:var(--panel);padding:10px;border-radius:6px}
    .markdown code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--panel);padding:1px 4px;border-radius:4px}
    .markdown blockquote{margin:0 0 1em;padding-left:12px;border-left:3px solid var(--line);color:var(--muted)}
    .markdown a,.markdown .internal-link{color:var(--accent)}
    .save-row{display:flex;align-items:center;gap:12px;padding-top:10px}
    .status{font-size:13px;color:var(--muted);overflow-wrap:anywhere}
    .status.error{color:var(--danger)}
    .right{display:flex;flex-direction:column;gap:18px}
    .panel{border-bottom:1px solid var(--line);padding-bottom:14px}
    .connections{display:flex;flex-direction:column;gap:5px}
    .connection{border:0;background:transparent;text-align:left;padding:5px 2px;color:var(--accent);overflow-wrap:anywhere}
    .connection:disabled{color:var(--muted)}
    .props{display:grid;grid-template-columns:minmax(70px,auto) 1fr;gap:3px 8px;font-size:12px}
    .props dt{color:var(--muted);overflow-wrap:anywhere}
    .props dd{margin:0;overflow-wrap:anywhere}
    .task-list{display:flex;flex-direction:column;gap:8px;margin-top:9px}
    .task{border:1px solid var(--line);border-radius:8px;padding:9px}
    .task p{margin:0 0 6px;overflow-wrap:anywhere}
    .task-foot{display:flex;align-items:center;gap:7px}
    .task-foot select{padding:4px 6px;font-size:12px}
    .task-id{font-size:11px;color:var(--muted);margin-left:auto}
    .stack-form{display:flex;gap:6px;margin-top:8px}
    .stack-form input{min-width:0}
    .checkpoint{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}
    form.checkpoint{display:grid;gap:6px;margin-top:8px}
    .checkpoint textarea{min-height:55px;resize:vertical}
    .empty{margin:auto;max-width:460px;padding:30px;text-align:center}
    .empty h1{font-size:24px}
    .flash{min-height:20px}
    .dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--text);width:min(520px,calc(100% - 30px));padding:20px}
    .dialog::backdrop{background:#1119}
    .dialog form{display:grid;gap:10px}
    .dialog textarea{min-height:170px;resize:vertical}
    .dialog footer{display:flex;justify-content:flex-end;gap:8px;margin-top:4px}

    @media(max-width:1050px){.workspace{grid-template-columns:220px minmax(280px,1fr)}
    .right{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;max-height:40vh}
    .right .panel{border:0}
    }

    @media(max-width:700px){.shell{height:auto;min-height:100vh}
    .workspace{grid-template-columns:1fr}
    .nav{max-height:32vh}
    .right{grid-column:auto;grid-template-columns:1fr;max-height:none}
    .editor-head{padding:15px}
    .editor{padding:14px}
    .toolbar{flex-direction:column}
    .top{padding:9px 12px}
    .sub{display:none}
    }

  </style>
</head>
<body>
<div class="shell">
  <header class="top"><div class="brand">CWM</div><div class="sub">Notes and agent memory</div><div class="spacer"></div><button id="refresh" class="quiet" type="button">Refresh</button></header>
  <div class="workspace">
    <aside class="nav" aria-label="Notes">
      <div class="navhead"><h2>Notes</h2><button id="new-note" class="primary" type="button">New note</button></div>
      <input id="search" class="search" type="text" placeholder="Search notes…" aria-label="Search notes">
      <div id="note-list" class="note-list"></div>
    </aside>
    <main id="main">
      <section id="welcome" class="empty"><h1>Your project knowledge</h1><p class="muted">Write durable notes here and keep them available to CWM’s CLI, hooks, and coding agents. Notes are Markdown files stored in this project.</p><button id="welcome-new" class="primary" type="button">Create a note</button></section>
      <section id="note-view" hidden>
        <header class="editor-head"><div class="titles"><h1 id="note-title"></h1><div id="note-tags" class="tags"></div></div><div class="toolbar"><button id="preview-toggle" type="button">Read view</button><button id="save-note" class="primary" type="button" disabled>Save</button></div></header>
        <div class="editor"><textarea id="note-content" aria-label="Markdown note content" spellcheck="true"></textarea><article id="note-preview" class="preview markdown" hidden></article><div class="save-row"><div id="note-status" class="status" role="status" aria-live="polite"></div><div class="spacer"></div><span id="note-path" class="status"></span></div></div>
      </section>
    </main>
    <aside class="right" aria-label="Note details and agent memory">
      <section class="panel"><h2>Note details</h2><dl id="note-properties" class="props"><dt class="muted">Select a note</dt></dl></section>
      <section class="panel"><h2>Connections</h2><h3>Links</h3><div id="outgoing" class="connections muted">Select a note</div><h3>Backlinks</h3><div id="backlinks" class="connections muted">Select a note</div></section>
      <section class="panel"><h2>Agent memory</h2><div id="checkpoint" class="checkpoint muted">No checkpoint saved.</div><form id="checkpoint-form" class="checkpoint"><h3>Save checkpoint</h3><input name="summary" type="text" placeholder="What changed?" maxlength="2000" required><input name="last" type="text" placeholder="Last action (optional)" maxlength="2000"><input name="next" type="text" placeholder="Next action (optional)" maxlength="2000"><button type="submit">Save checkpoint</button></form></section>
      <section class="panel"><h2>Active tasks</h2><form id="task-form" class="stack-form"><input name="description" type="text" placeholder="Add a task…" maxlength="4000" required><button type="submit" class="primary">Add</button></form><div id="task-list" class="task-list"></div></section>
    </aside>
  </div>
</div>
<dialog id="create-dialog" class="dialog"><form id="create-form"><h2>Create a project note</h2><label>Title<input name="title" type="text" maxlength="300" required autofocus></label><label>Tags<input name="tags" type="text" placeholder="e.g. design, decisions"></label><label>Note<textarea name="content" placeholder="Start writing in Markdown…"></textarea></label><footer><button id="cancel-create" type="button">Cancel</button><button class="primary" type="submit">Create note</button></footer></form></dialog>
<script nonce="{{NONCE}}">const CWM_TOKEN={{TOKEN}};</script>
<script nonce="{{NONCE}}">
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {notes: [], current: null, linkMap: [], preview: false, dirty: false, saving: false, timer: null};
  const api = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (options.method) { headers.set('Content-Type', 'application/json'); headers.set('X-CWM-Token', CWM_TOKEN); }
    const response = await fetch(path, {...options, headers, credentials: 'same-origin', cache: 'no-store'});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  };
  const flash = (message, error = false) => { const el = $('note-status'); el.textContent = message; el.classList.toggle('error', error); };
  const divMessage = (target, message) => { target.textContent = message; };
  const noteButton = (note) => {
    const button = document.createElement('button'); button.className = 'note-item' + (state.current && state.current.id === note.id ? ' active' : ''); button.type = 'button';
    const title = document.createElement('strong'); title.textContent = note.title;
    const meta = document.createElement('small'); meta.textContent = `${note.tags.length ? '#' + note.tags.join(' #') + ' · ' : ''}${note.path}`;
    button.append(title, meta); button.addEventListener('click', () => openNote(note.id)); return button;
  };
  const internalPath = (destination) => {
    let path = destination.split(/[?#]/, 1)[0];
    try { path = decodeURIComponent(path); } catch { return null; }
    if (!path || path.startsWith('/') || /^[a-z][a-z0-9+.-]*:/i.test(path) || path.startsWith('//')) return null;
    const parts = (state.current?.path || '').split('/'); parts.pop();
    for (const part of path.replace(/\\/g, '/').split('/')) {
      if (!part || part === '.') continue;
      if (part === '..') { if (!parts.length) return null; parts.pop(); }
      else parts.push(part);
    }
    const resolved = parts.join('/');
    if (!resolved || (!/\.md$/i.test(resolved) && /(?:^|\/)[^/]+\.[^/]+$/.test(resolved))) return null;
    return resolved;
  };
  function inlineMarkdown(parent, text) {
    const pattern = /(\[\[[^\]]+\]\]|\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*]+)\*)/g;
    let cursor = 0; let match;
    while ((match = pattern.exec(text))) {
      if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
      const token = match[0];
      if (token.startsWith('[[')) {
        const raw = token.slice(2, -2); const [reference, alias] = raw.split('|', 2); const link = document.createElement('button'); link.type = 'button'; link.className = 'internal-link quiet'; link.textContent = alias || reference;
        link.addEventListener('click', () => openNote(reference.split('#', 1)[0])); parent.append(link);
      } else if (match[2] !== undefined) {
        const destination = match[3]; const hasScheme = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(destination);
        const indexed = state.linkMap.find(item => item.target === destination && item.resolved);
        const target = indexed ? indexed.resolved.id : (!hasScheme ? internalPath(destination) : null);
        if (target) { const link = document.createElement('button'); link.type = 'button'; link.className = 'internal-link quiet'; link.textContent = match[2]; link.addEventListener('click', () => openNote(target)); parent.append(link); }
        else {
          let url;
          try { url = new URL(destination, location.origin); } catch { url = null; }
          if (hasScheme && url && ['https:','http:','mailto:'].includes(url.protocol)) { const anchor = document.createElement('a'); anchor.textContent = match[2]; anchor.href = url.href; anchor.rel = 'noopener noreferrer'; anchor.target = '_blank'; parent.append(anchor); }
          else parent.append(document.createTextNode(match[2]));
        }
      } else if (match[4] !== undefined) { const code = document.createElement('code'); code.textContent = match[4]; parent.append(code); }
      else if (match[5] !== undefined || match[6] !== undefined) { const strong = document.createElement('strong'); strong.textContent = match[5] || match[6]; parent.append(strong); }
      else { const em = document.createElement('em'); em.textContent = match[7]; parent.append(em); }
      cursor = pattern.lastIndex;
    }
    if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
  }
  function renderMarkdown(source) {
    const root = $('note-preview'); root.replaceChildren(); const lines = source.split(/\r?\n/); let i = 0;
    const firstContentLine = lines.findIndex(line => line.trim());
    if (state.current && firstContentLine >= 0 && lines[firstContentLine].trim() === '# ' + state.current.title) lines.splice(firstContentLine, 1);
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (/^\s*```/.test(line)) {
        i++; const code = document.createElement('pre'); const content = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) content.push(lines[i++]);
        if (i < lines.length) i++; code.textContent = content.join('\n'); root.append(code); continue;
      }
      const heading = /^( {0,3})(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) { const element = document.createElement('h' + heading[2].length); inlineMarkdown(element, heading[3]); root.append(element); i++; continue; }
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { root.append(document.createElement('hr')); i++; continue; }
      if (/^\s*>/.test(line)) { const quote = document.createElement('blockquote'); while (i < lines.length && /^\s*>/.test(lines[i])) { const p = document.createElement('p'); inlineMarkdown(p, lines[i++].replace(/^\s*>\s?/, '')); quote.append(p); } root.append(quote); continue; }
      if (/^\s*[-+*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line); const list = document.createElement(ordered ? 'ol' : 'ul');
        while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/).test(lines[i])) { const li = document.createElement('li'); inlineMarkdown(li, lines[i++].replace(ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/, '')); list.append(li); }
        root.append(list); continue;
      }
      const paragraph = document.createElement('p'); inlineMarkdown(paragraph, lines[i++]);
      while (i < lines.length && lines[i].trim() && !/^( {0,3}#{1,6}\s|\s*```|\s*>|\s*[-+*]\s+|\s*\d+[.)]\s+)/.test(lines[i])) { paragraph.append(document.createElement('br')); inlineMarkdown(paragraph, lines[i++]); }
      root.append(paragraph);
    }
  }
  async function loadNotes(query = $('search').value) {
    const target = $('note-list'); target.replaceChildren();
    try {
      const data = await api('/api/notes?q=' + encodeURIComponent(query)); state.notes = data.notes;
      if (!state.notes.length) { const empty = document.createElement('div'); empty.className = 'muted'; empty.textContent = query ? 'No matching notes.' : 'No notes yet.'; target.append(empty); return; }
      for (const note of state.notes) target.append(noteButton(note));
    } catch (error) { const item = document.createElement('div'); item.className = 'status error'; item.textContent = error.message; target.append(item); }
  }
  const propertyDisplay = (note) => {
    const list = $('note-properties'); list.replaceChildren();
    const values = [['File', note.path], ['Updated', note.updated], ['Aliases', note.aliases.join(', ') || '—']];
    for (const [key, value] of Object.entries(note.properties || {})) if (!['id','title','created','updated','tags','aliases'].includes(key.toLowerCase())) values.push([key, Array.isArray(value) ? value.join(', ') : String(value)]);
    for (const [key, value] of values) { const dt = document.createElement('dt'); dt.textContent = key; const dd = document.createElement('dd'); dd.textContent = String(value); list.append(dt, dd); }
  };
  const connectionItems = (target, items, emptyText) => {
    target.replaceChildren(); target.classList.toggle('muted', !items.length);
    if (!items.length) { target.textContent = emptyText; return; }
    for (const item of items) {
      const button = document.createElement('button'); button.className = 'connection'; button.type = 'button'; button.textContent = item.title || item.target;
      if (item.id) button.addEventListener('click', () => openNote(item.id)); else button.disabled = true;
      target.append(button);
    }
  };
  async function openNote(reference) {
    if (state.saving) { flash('Wait for the current note save to finish.', true); return; }
    if (state.dirty && !window.confirm('Discard unsaved changes and open another note?')) return;
    try {
      const note = await api('/api/note?ref=' + encodeURIComponent(reference)); state.current = note; state.linkMap = []; state.dirty = false; state.preview = false;
      $('welcome').hidden = true; $('note-view').hidden = false; $('note-title').textContent = note.title; $('note-content').value = note.content;
      $('note-content').hidden = false; $('note-preview').hidden = true; $('preview-toggle').textContent = 'Read view'; $('save-note').disabled = true;
      $('note-path').textContent = note.path; flash('');
      const tags = $('note-tags'); tags.replaceChildren(); for (const tag of note.tags) { const span = document.createElement('span'); span.className = 'tag'; span.textContent = '#' + tag; tags.append(span); }
      propertyDisplay(note); await Promise.all([loadConnections(note.id), loadNotes()]);
    } catch (error) { flash(error.message, true); }
  }
  async function loadConnections(reference) {
    try {
      const [links, backlinks] = await Promise.all([api('/api/links?ref=' + encodeURIComponent(reference)), api('/api/backlinks?ref=' + encodeURIComponent(reference))]);
      if (!state.current || state.current.id !== reference) return;
      state.linkMap = links.links;
      connectionItems($('outgoing'), links.links.map(x => x.resolved ? {...x.resolved} : {target: x.target}), 'No outgoing links.');
      connectionItems($('backlinks'), backlinks.notes, 'No backlinks yet.');
    } catch (error) { divMessage($('outgoing'), error.message); }
  }
  async function loadMemory() {
    try {
      const data = await api('/api/memory');
      const cp = $('checkpoint'); cp.replaceChildren();
      if (!data.checkpoint) cp.textContent = 'No checkpoint saved.';
      else { const summary = document.createElement('strong'); summary.textContent = data.checkpoint.summary; cp.append(summary); if (data.checkpoint.last_action) { const last = document.createElement('div'); last.textContent = 'Last: ' + data.checkpoint.last_action; cp.append(last); } if (data.checkpoint.next_action) { const next = document.createElement('div'); next.textContent = 'Next: ' + data.checkpoint.next_action; cp.append(next); } const stamp = document.createElement('small'); stamp.className = 'muted'; stamp.textContent = data.checkpoint.created_at; cp.append(stamp); }
      const tasks = $('task-list'); tasks.replaceChildren();
      if (!data.tasks.length) { tasks.textContent = 'No active tasks.'; tasks.className = 'task-list muted'; return; }
      tasks.className = 'task-list';
      for (const task of data.tasks) {
        const card = document.createElement('div'); card.className = 'task'; const description = document.createElement('p'); description.textContent = task.description; card.append(description);
        const foot = document.createElement('div'); foot.className = 'task-foot'; const select = document.createElement('select'); select.setAttribute('aria-label', 'Task status');
        for (const status of ['pending','in_progress','blocked','completed','cancelled']) { const option = document.createElement('option'); option.value = status; option.textContent = status.replace('_',' '); option.selected = status === task.status; select.append(option); }
        select.addEventListener('change', async () => { let evidence = ''; if (select.value === 'completed') { evidence = window.prompt('Completion evidence (for example, test: checks passed and output reviewed)') || ''; if (!evidence) { select.value = task.status; return; } } try { await api('/api/task/status', {method:'POST', body:JSON.stringify({id:task.id,status:select.value,evidence})}); await loadMemory(); } catch (error) { window.alert(error.message); select.value = task.status; } });
        const id = document.createElement('span'); id.className = 'task-id'; id.textContent = task.id; foot.append(select,id); card.append(foot); tasks.append(card);
      }
    } catch (error) { divMessage($('task-list'), error.message); }
  }
  $('search').addEventListener('input', () => { clearTimeout(state.timer); state.timer = setTimeout(() => loadNotes(), 180); });
  $('refresh').addEventListener('click', async () => { await Promise.all([loadNotes(), loadMemory()]); if (state.current) await openNote(state.current.id); });
  $('new-note').addEventListener('click', () => $('create-dialog').showModal()); $('welcome-new').addEventListener('click', () => $('create-dialog').showModal());
  $('cancel-create').addEventListener('click', () => $('create-dialog').close());
  $('create-form').addEventListener('submit', async (event) => {
    event.preventDefault(); const form = new FormData(event.currentTarget); const tags = String(form.get('tags') || '').split(',').map(x => x.trim()).filter(Boolean);
    const createForm = event.currentTarget;
    try { const note = await api('/api/note/create', {method:'POST', body:JSON.stringify({title:form.get('title'),tags,content:form.get('content')})}); $('create-dialog').close(); createForm.reset(); await loadNotes(); await openNote(note.id); }
    catch (error) { window.alert(error.message); }
  });
  $('note-content').addEventListener('input', () => { state.dirty = true; $('save-note').disabled = state.saving; flash(state.saving ? 'Editing while the previous version saves…' : 'Unsaved changes'); });
  $('preview-toggle').addEventListener('click', () => { state.preview = !state.preview; $('note-content').hidden = state.preview; $('note-preview').hidden = !state.preview; $('preview-toggle').textContent = state.preview ? 'Edit source' : 'Read view'; if (state.preview) renderMarkdown($('note-content').value); });
  $('save-note').addEventListener('click', async () => {
    if (!state.current || state.saving) return;
    const content = $('note-content').value; const revision = state.current.revision; state.saving = true; $('save-note').disabled = true; flash('Saving…');
    try {
      const updated = await api('/api/note/save', {method:'POST', body:JSON.stringify({reference:state.current.id,content,revision})}); state.current = updated; state.linkMap = []; state.saving = false;
      if ($('note-content').value === content) { $('note-content').value = updated.content; state.dirty = false; $('save-note').disabled = true; flash('Saved'); }
      else { state.dirty = true; $('save-note').disabled = false; flash('Saved. Newer edits still need saving.'); }
      if (state.preview) renderMarkdown($('note-content').value);
      $('note-path').textContent = updated.path; propertyDisplay(updated); await Promise.all([loadNotes(),loadConnections(updated.id)]);
    } catch (error) { state.saving = false; $('save-note').disabled = false; flash(error.message + ' Your editor contents are still here.', true); }
  });
  $('task-form').addEventListener('submit', async (event) => { event.preventDefault(); const input = event.currentTarget.elements.description; try { await api('/api/task', {method:'POST',body:JSON.stringify({description:input.value})}); input.value = ''; await loadMemory(); } catch (error) { window.alert(error.message); } });
  $('checkpoint-form').addEventListener('submit', async (event) => { event.preventDefault(); const formElement = event.currentTarget; const form = new FormData(formElement); try { await api('/api/checkpoint', {method:'POST',body:JSON.stringify({summary:form.get('summary'),last:form.get('last'),next:form.get('next')})}); formElement.reset(); await loadMemory(); } catch (error) { window.alert(error.message); } });
  loadNotes(); loadMemory();
})();
</script>
</body>
</html>'''


def serve(store: Store, port: int = 8765, open_browser: bool = True) -> int:
    """Serve the notes UI on loopback; the caller owns and closes the store."""
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    vault = ProjectVault(store.path.parent / "vault")
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CWM"
        sys_version = ""

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if content_type.startswith("text/html"):
                nonce = self.page_nonce
                self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            raw = self.headers.get("Host", "")
            try:
                parsed = urlsplit("http://" + raw)
                request_port = parsed.port if parsed.port is not None else 80
                return parsed.hostname in {"127.0.0.1", "localhost"} and request_port == self.server.server_port and parsed.username is None and parsed.password is None and not parsed.path and not parsed.query and not parsed.fragment
            except ValueError:
                return False

        def _write_authorized(self) -> bool:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            return (
                self._host_ok()
                and origin == "http://" + host
                and hmac.compare_digest(self.headers.get("X-CWM-Token", ""), token)
                and self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() == "application/json"
            )

        def _body(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid Content-Length") from error
            if length < 0 or length > 700_000:
                raise ValueError("Request body must be at most 700,000 bytes")
            try:
                value = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError("Request body must contain valid JSON") from error
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object")
            return value

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "This local CWM server only accepts loopback requests."})
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                self.page_nonce = secrets.token_urlsafe(18)
                page = _PAGE.replace("{{NONCE}}", self.page_nonce).replace("{{TOKEN}}", json.dumps(token))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/notes":
                    search = query.get("q", [""])[0]
                    notes = vault.search_notes(search, 100) if search.strip() else vault.list_notes(limit=100)
                    self._json(200, {"notes": notes})
                elif parsed.path == "/api/note":
                    reference = query.get("ref", [""])[0]
                    if not reference:
                        raise ValueError("A note reference is required")
                    note = vault.read_note(reference, 100_000)
                    if note["truncated"]:
                        self._json(413, {"error": "This note is too large to edit in the CWM app; open it in a Markdown editor."})
                    else:
                        self._json(200, note)
                elif parsed.path == "/api/links":
                    self._json(200, {"links": vault.note_links(_required(query, "ref"))})
                elif parsed.path == "/api/backlinks":
                    self._json(200, {"notes": vault.backlinks(_required(query, "ref"))})
                elif parsed.path == "/api/memory":
                    active = store.list_tasks(["pending", "in_progress", "blocked"])
                    self._json(200, {"tasks": active[:100], "checkpoint": store.latest_checkpoint()})
                else:
                    self._json(404, {"error": "Not found"})
            except KeyError as error:
                self._json(404, {"error": str(error).strip("'")})
            except ValueError as error:
                self._json(400, {"error": str(error)})

        def do_POST(self) -> None:
            if not self._write_authorized():
                self._json(403, {"error": "Write request rejected. Reload the local CWM page and try again."})
                return
            try:
                payload = self._body()
                path = urlsplit(self.path).path
                if path == "/api/note/create":
                    content = _string(payload, "content", default="")
                    if len(content) > 100_000:
                        raise ValueError("Note content must be no longer than 100,000 characters")
                    note = vault.create_note(
                        _string(payload, "title"), content,
                        _string_list(payload, "tags"), [], {},
                    )
                    self._json(201, note)
                elif path == "/api/note/save":
                    note = vault.replace_note_body(_string(payload, "reference"), _string(payload, "content"), _string(payload, "revision"))
                    self._json(200, note)
                elif path == "/api/task":
                    task_id, created = store.add_task(_string(payload, "description"))
                    self._json(201, {"id": task_id, "created": created})
                elif path == "/api/task/status":
                    store.set_status(_string(payload, "id"), _string(payload, "status"), _string(payload, "evidence", default=""))
                    self._json(200, {"updated": True})
                elif path == "/api/checkpoint":
                    checkpoint_id = store.checkpoint(_string(payload, "summary"), _string(payload, "last", default=""), _string(payload, "next", default=""))
                    self._json(201, {"id": checkpoint_id})
                else:
                    self._json(404, {"error": "Not found"})
            except KeyError as error:
                self._json(404, {"error": str(error).strip("'")})
            except RuntimeError as error:
                self._json(409, {"error": str(error)})
            except ValueError as error:
                self._json(400, {"error": str(error)})

        def log_message(self, format: str, *args: object) -> None:
            sys.stderr.write("cwm-ui: " + (format % args) + "\n")

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as error:
        print(f"Could not start CWM UI on 127.0.0.1:{port}: {error}", file=sys.stderr)
        return 1
    address, actual_port = server.server_address[:2]
    url = f"http://{address}:{actual_port}/"
    print(f"CWM notes and agent memory: {url}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCWM UI stopped.", flush=True)
    finally:
        server.server_close()
    return 0


def _required(query: dict[str, list[str]], key: str) -> str:
    value = query.get(key, [""])[0]
    if not value:
        raise ValueError(f"A {key} value is required")
    return value


def _string(payload: dict[str, object], key: str, default: str | None = None) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _string_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of text values")
    return value
