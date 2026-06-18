#!/usr/bin/env node
// Send a compact Agent Broker callback into the OpenAI/Codex webview over CDP.
// It targets only extensionId=openai.chatgpt and fails closed if the composer
// is not visible or already contains text.

import { readFileSync } from 'node:fs';

const argv = process.argv.slice(2);
function arg(name, fallback = '') {
  const idx = argv.indexOf(`--${name}`);
  if (idx < 0 || idx + 1 >= argv.length) return fallback;
  return String(argv[idx + 1]).startsWith('--') ? fallback : argv[idx + 1];
}

const textFile = arg('text-file', '');
let text = arg('text', '');
if (textFile) {
  try { text = readFileSync(textFile, 'utf8'); } catch {}
}
const port = Number(arg('port', process.env.AGENT_BROKER_CDP_PORT || '9000'));
const timeoutMs = Number(arg('timeout', '6000'));

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function fetchJson(url) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, { signal: controller.signal });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let nextId = 1;
    const pending = new Map();
    const events = [];
    const timer = setTimeout(() => { try { ws.close(); } catch {} reject(new Error('cdp connect timeout')); }, timeoutMs);
    ws.onopen = () => {
      clearTimeout(timer);
      resolve({
        events,
        close: () => ws.close(),
        send(method, params = {}) {
          const id = nextId++;
          return new Promise((res, rej) => {
            pending.set(id, { res, rej });
            ws.send(JSON.stringify({ id, method, params }));
          });
        },
      });
    };
    ws.onerror = () => { clearTimeout(timer); reject(new Error('cdp ws error')); };
    ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id && pending.has(m.id)) {
        const it = pending.get(m.id);
        pending.delete(m.id);
        m.error ? it.rej(new Error(m.error.message || JSON.stringify(m.error))) : it.res(m.result || {});
        return;
      }
      if (m.method) events.push(m);
    };
  });
}

const DETECT = `
(() => {
  const norm = s => String(s == null ? '' : s).replace(/\\s+/g, ' ').trim();
  const visible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || '1') > 0;
  };
  const isComposer = el => {
    if (!el || !el.tagName) return false;
    if (!(el.isContentEditable || el.tagName.toLowerCase() === 'textarea')) return false;
    const raw = norm([el.className, el.getAttribute('role'), el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('data-placeholder')].join(' ')).toLowerCase();
    return el.isContentEditable || /message|ask|prompt|chat|composer|prosemirror/.test(raw);
  };
  const rectOf = el => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; };
  const active = document.activeElement;
  let comp = null, focused = false;
  if (isComposer(active) && visible(active)) { comp = active; focused = true; }
  if (!comp) {
    const cands = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""]'))
      .filter(el => isComposer(el) && visible(el))
      .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
    comp = cands[0] || null;
  }
  if (!comp) return { found: false };
  const cur = norm(comp.value !== undefined ? comp.value : comp.innerText);
  return { found: true, focused, empty: cur.length === 0, curLen: cur.length, coords: rectOf(comp), tag: comp.tagName, className: String(comp.className || ''), aria: (comp.getAttribute && comp.getAttribute('aria-label')) || '' };
})()`;

const READ_BACK = `
(() => {
  const a = document.activeElement;
  const t = a && (a.value !== undefined ? a.value : a.innerText) || '';
  return String(t).replace(/\\s+/g,' ').trim().length;
})()`;

async function evaluate(cdp, expression, contextId = undefined) {
  const params = { expression, returnByValue: true, userGesture: true };
  if (contextId) params.contextId = contextId;
  const r = await cdp.send('Runtime.evaluate', params);
  return r.result && r.result.value;
}

async function clickAt(cdp, x, y) {
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
}

async function pressEnter(cdp) {
  const base = { windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, key: 'Enter', code: 'Enter' };
  await cdp.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base });
  await cdp.send('Input.dispatchKeyEvent', { type: 'char', ...base, text: '\r', unmodifiedText: '\r' });
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
}

async function tryTarget(target) {
  let cdp;
  try { cdp = await connect(target.webSocketDebuggerUrl); } catch { return null; }
  try {
    await cdp.send('Runtime.enable');
    try { await cdp.send('Page.enable'); } catch {}
    await sleep(120);
    const contexts = cdp.events
      .filter(e => e.method === 'Runtime.executionContextCreated')
      .map(e => e.params && e.params.context)
      .filter(Boolean);
    const contextIds = [undefined, ...contexts.map(ctx => ctx.id).filter(Boolean)];
    const detections = [];
    for (const contextId of contextIds) {
      try {
        const det = await evaluate(cdp, DETECT, contextId);
        if (det && det.found) detections.push({ det, contextId });
      } catch {}
    }
    if (!detections.length) return { cdp, det: { found: false } };
    detections.sort((a, b) => {
      const sa = (a.det.focused ? 4 : 0) + (a.det.empty ? 2 : 0) + (a.contextId ? 1 : 0);
      const sb = (b.det.focused ? 4 : 0) + (b.det.empty ? 2 : 0) + (b.contextId ? 1 : 0);
      return sb - sa;
    });
    const { det, contextId } = detections[0];
    if (!det.empty) return { cdp, det, contextId, error: 'Codex composer not empty; refusing to overwrite current user text' };
    await clickAt(cdp, det.coords.x, det.coords.y);
    await sleep(120);
    await cdp.send('Input.insertText', { text });
    await sleep(150);
    await pressEnter(cdp);
    await sleep(350);
    let lenAfter = -1;
    try { lenAfter = await evaluate(cdp, READ_BACK, contextId); } catch {}
    return { cdp, det, contextId, typed: true, submitted: lenAfter === 0, lenAfter, type: target.type };
  } catch (err) {
    return { cdp, error: err.message || String(err) };
  } finally {
    try { cdp.close(); } catch {}
  }
}

async function main() {
  if (!text.trim()) {
    console.log(JSON.stringify({ ok: false, reason: 'empty text' }));
    return;
  }
  const isCodexTarget = t => {
    const raw = `${t.type || ''} ${t.title || ''} ${t.url || ''}`.toLowerCase();
    return t.webSocketDebuggerUrl && ['iframe', 'webview'].includes(t.type) && raw.includes('extensionid=openai.chatgpt');
  };
  const targets = (await fetchJson(`http://127.0.0.1:${port}/json/list`)).filter(isCodexTarget);
  if (!targets.length) {
    console.log(JSON.stringify({ ok: false, reason: 'no OpenAI/Codex webview CDP target found' }));
    return;
  }
  const found = [];
  for (const target of targets) {
    const res = await tryTarget(target);
    if (res && res.det && res.det.found) found.push({ target, res });
  }
  if (!found.length) {
    console.log(JSON.stringify({ ok: false, reason: 'no Codex composer found' }));
    return;
  }
  found.sort((a, b) => ((b.res.det.focused ? 2 : 0) + (b.res.det.empty ? 1 : 0)) - ((a.res.det.focused ? 2 : 0) + (a.res.det.empty ? 1 : 0)));
  const best = found[0].res;
  if (best.error) {
    console.log(JSON.stringify({ ok: false, reason: best.error, focused: best.det && best.det.focused, aria: best.det && best.det.aria }));
    return;
  }
  console.log(JSON.stringify({
    ok: !!best.submitted,
    type: best.type,
    focused: best.det.focused,
    contextId: best.contextId,
    typed: !!best.typed,
    submitted: !!best.submitted,
    lenAfter: best.lenAfter,
    className: best.det.className,
  }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, reason: err.message || String(err) }));
  process.exitCode = 1;
});
