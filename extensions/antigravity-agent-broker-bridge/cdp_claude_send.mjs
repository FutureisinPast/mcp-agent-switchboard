#!/usr/bin/env node
// Set text into Claude's webview composer and submit it, over CDP.
//
// Why CDP Input (not DOM value/execCommand): Claude's composer is a rich
// (React/Lexical) editor whose Send button stays DISABLED until the editor's
// own state sees text. Programmatic value/innerText edits often don't update
// that state, so the button never enables. CDP Input.insertText types as if
// from the keyboard, which the editor registers, enabling submit. Submit is a
// real CDP Enter key event (Claude defaults to Enter-to-send).
//
// Targeting: Claude renders in a VS Code webview IFRAME whose target URL/title
// contains extensionId=Anthropic.claude-code. Never fall back to Antigravity's
// top PAGE composer; failing closed is safer than sending to the wrong agent.

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
  try { text = readFileSync(textFile, 'utf8'); } catch (e) { /* fall back to --text */ }
}
const port = Number(arg('port', process.env.AGENT_BROKER_CDP_PORT || '9000'));
const timeoutMs = Number(arg('timeout', '6000'));
const setText = arg('set-text', 'true') !== 'false';
const submit = arg('submit', 'true') !== 'false';

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
    const tag = el.tagName.toLowerCase();
    if (!(el.isContentEditable || tag === 'textarea')) return false;
    const aria = ((el.getAttribute && el.getAttribute('aria-label')) || '').toLowerCase();
    const ph = ((el.getAttribute && (el.getAttribute('placeholder') || el.getAttribute('data-placeholder'))) || '').toLowerCase();
    return /message|reply|ask|prompt|chat|claude/.test(aria + ' ' + ph) || tag === 'textarea';
  };
  const rectOf = el => { const r = el.getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; };
  const buttonInfo = el => {
    const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true';
    return { enabled: !disabled, coords: rectOf(el), disabled };
  };
  const bodyText = norm(document.body && document.body.innerText);
  const active = document.activeElement;
  let comp = null, focused = false;
  if (isComposer(active) && visible(active)) { comp = active; focused = true; }
  if (!comp) {
    const cands = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [contenteditable=""]'))
      .filter(el => isComposer(el) && visible(el))
      .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
    comp = cands[0] || null;
  }
  if (!comp) return { found: false };
  const cur = norm(comp.value !== undefined ? comp.value : comp.innerText);
  const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
  const send = buttons
    .filter(el => {
      const raw = norm([
        el.className,
        el.getAttribute('type'),
        el.getAttribute('aria-label'),
        el.getAttribute('title'),
        el.innerText,
      ].join(' ')).toLowerCase();
      return raw.includes('send') || raw.includes('submit');
    })
    .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0] || null;
  const hasDraft = /Agent Broker Routed Request|Claude Inbox Request|Request ID:|Safe Claude-extension routing test/i.test(bodyText);
  return {
    found: true,
    focused,
    empty: cur.length === 0,
    curLen: cur.length,
    hasDraft,
    send: send ? buttonInfo(send) : null,
    coords: rectOf(comp),
    tag: comp.tagName,
    aria: (comp.getAttribute && comp.getAttribute('aria-label')) || ''
  };
})()`;

function readBackExpr() {
  return `(() => {
    const a = document.activeElement;
    const t = a && (a.value !== undefined ? a.value : a.innerText) || '';
    return String(t).replace(/\\s+/g,' ').trim().length;
  })()`;
}

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
    await sleep(80);
    const contextEvents = cdp.events
      .filter(e => e.method === 'Runtime.executionContextCreated')
      .map(e => e.params && e.params.context)
      .filter(Boolean);
    const contextIds = [undefined, ...contextEvents.map(ctx => ctx.id).filter(Boolean)];
    const detections = [];
    for (const contextId of contextIds) {
      try {
        const det = await evaluate(cdp, DETECT, contextId);
        if (det && det.found) detections.push({ det, contextId });
      } catch {}
    }
    if (!detections.length) return { cdp, det: { found: false } };
    detections.sort((a, b) => {
      const sa = (a.det.focused ? 4 : 0) + (!a.det.empty ? 2 : 0) + (a.contextId ? 1 : 0);
      const sb = (b.det.focused ? 4 : 0) + (!b.det.empty ? 2 : 0) + (b.contextId ? 1 : 0);
      return sb - sa;
    });
    const { det, contextId } = detections[0];
    if (!det || !det.found) return { cdp, det };
    if (setText && text && !det.empty) {
      return { cdp, det, error: 'composer not empty; refusing to overwrite or submit existing text' };
    }
    if (submit && !setText && det.empty && !(det.hasDraft && det.send && det.send.enabled)) {
      return { cdp, det, error: 'composer empty and no enabled Claude draft send button found' };
    }
    // Focus the composer (it usually already is; click ensures it for insertText).
    await clickAt(cdp, det.coords.x, det.coords.y);
    await sleep(120);
    let typed = false;
    if (setText && text && det.empty) {
      await cdp.send('Input.insertText', { text });
      typed = true;
      await sleep(150);
    }
    let submitted = false, lenAfter = -1;
    if (submit) {
      let submitDet = det;
      try {
        const refreshed = await evaluate(cdp, DETECT, contextId);
        if (refreshed && refreshed.found) submitDet = refreshed;
      } catch {}
      if (submitDet.send && submitDet.send.enabled) {
        await clickAt(cdp, submitDet.send.coords.x, submitDet.send.coords.y);
        submitted = true;
      } else {
        await pressEnter(cdp);
      }
      await sleep(250);
      try { lenAfter = await evaluate(cdp, readBackExpr(), contextId); } catch {}
      // If the composer emptied out, the message was sent.
      submitted = submitted || (typed ? lenAfter === 0 : true);
    }
    return { cdp, det, contextId, typed, submitted, lenAfter, type: target.type, title: (target.title || '').slice(0, 80), url: (target.url || '').slice(0, 160) };
  } catch (err) {
    return { cdp, error: err.message || String(err) };
  } finally {
    try { cdp.close(); } catch {}
  }
}

async function main() {
  const isClaudeTarget = t => {
    const raw = `${t.type || ''} ${t.title || ''} ${t.url || ''}`.toLowerCase();
    return t.webSocketDebuggerUrl
      && ['iframe', 'webview'].includes(t.type)
      && raw.includes('extensionid=anthropic.claude-code');
  };
  const all = (await fetchJson(`http://127.0.0.1:${port}/json/list`))
    .filter(isClaudeTarget);
  if (!all.length) {
    console.log(JSON.stringify({ ok: false, reason: 'no Claude Code webview CDP target found' }));
    return;
  }
  const targets = all;

  const found = [];
  for (const t of targets) {
    const res = await tryTarget(t);
    if (res && res.det && res.det.found) {
      found.push({ t, res });
      if (res.det.focused) break;
    }
  }
  if (!found.length) {
    console.log(JSON.stringify({ ok: false, reason: 'no composer found in Claude Code webview target' }));
    return;
  }
  // Prefer a webview hit with focus, else first.
  found.sort((a, b) => {
    const sa = (a.res.det.focused ? 2 : 0) + (a.t.type !== 'page' ? 1 : 0);
    const sb = (b.res.det.focused ? 2 : 0) + (b.t.type !== 'page' ? 1 : 0);
    return sb - sa;
  });
  const best = found[0].res;
  if (best.error) {
    console.log(JSON.stringify({
      ok: false,
      reason: best.error,
      type: best.type,
      focused: best.det && best.det.focused,
      aria: best.det && best.det.aria,
    }));
    return;
  }
  console.log(JSON.stringify({
    ok: submit ? !!best.submitted : true,
    type: best.type,
    focused: best.det.focused,
    contextId: best.contextId,
    typed: !!best.typed,
    submitted: !!best.submitted,
    lenAfter: best.lenAfter,
    aria: best.det.aria,
  }));
}

main().catch(err => {
  console.log(JSON.stringify({ ok: false, reason: err.message || String(err) }));
  process.exitCode = 1;
});
