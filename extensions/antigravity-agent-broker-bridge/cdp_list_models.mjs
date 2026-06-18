#!/usr/bin/env node

const argv = process.argv.slice(2);

function arg(name, fallback = '') {
  const idx = argv.indexOf(`--${name}`);
  if (idx >= 0 && idx + 1 < argv.length) {
    return argv[idx + 1];
  }
  return fallback;
}

const port = Number(arg('port', process.env.AGENT_BROKER_CDP_PORT || '9000'));
const timeoutMs = Number(arg('timeout', '5000'));

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function fetchJson(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function chooseTarget(targets) {
  const pages = targets.filter(target => target.webSocketDebuggerUrl && target.type === 'page');
  const scored = pages.map(target => {
    const haystack = `${target.title || ''} ${target.url || ''}`.toLowerCase();
    let score = 0;
    if (haystack.includes('workbench.html')) score += 150;
    if (haystack.includes('workbench-jetski-agent.html')) score -= 100;
    if ((target.title || '').toLowerCase() === 'launchpad') score -= 100;
    if (haystack.includes('antigravity')) score += 50;
    if (haystack.includes('workbench')) score += 30;
    return { target, score };
  });
  scored.sort((a, b) => b.score - a.score);
  return scored[0] ? scored[0].target : pages[0];
}

function connectCdp(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let nextId = 1;
    const pending = new Map();
    ws.onopen = () => {
      resolve({
        close: () => ws.close(),
        send(method, params = {}) {
          const id = nextId++;
          ws.send(JSON.stringify({ id, method, params }));
          return new Promise((innerResolve, innerReject) => {
            pending.set(id, { resolve: innerResolve, reject: innerReject });
          });
        },
      });
    };
    ws.onerror = () => reject(new Error('CDP websocket error'));
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (!message.id || !pending.has(message.id)) return;
      const item = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) item.reject(new Error(message.error.message || JSON.stringify(message.error)));
      else item.resolve(message.result || {});
    };
  });
}

function extractExpression() {
  return `
(() => {
  const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 2 && rect.height > 2 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const read = el => normalize([
    el.innerText,
    el.textContent,
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('title'),
  ].filter(Boolean).join(' '));
  const modelPattern = /\\b(gemini|claude|gpt|sonnet|opus|haiku|flash|pro|thinking|oss)\\b/i;
  const exactModelPattern = /(Gemini\\s+[0-9.]+\\s+(?:Flash|Pro)\\s+\\([^)]+\\)|Claude\\s+(?:Sonnet|Opus|Haiku)\\s+[0-9.]+\\s+\\([^)]+\\)|GPT-OSS\\s+[0-9A-Z]+\\s+\\([^)]+\\))/g;
  const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
  let current = '';
  let opener = null;
  for (const button of buttons) {
    const text = read(button);
    if (/select model/i.test(text) || modelPattern.test(text)) {
      if (!opener || /select model/i.test(text)) opener = button;
      if (/select model/i.test(text)) {
        const match = text.match(/current:\\s*(.+)$/i);
        current = match ? normalize(match[1]) : text.replace(/select model, current:/i, '').trim();
      }
    }
  }
  const allText = normalize(document.body ? document.body.innerText : '');
  const exactMatches = Array.from(allText.matchAll(exactModelPattern)).map(match => normalize(match[1] || match[0]));
  const buttonMatches = buttons.flatMap(button => {
    const text = read(button);
    return Array.from(text.matchAll(exactModelPattern)).map(match => normalize(match[1] || match[0]));
  });
  return {
    current,
    opener: opener ? (() => {
      const rect = opener.getBoundingClientRect();
      return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2), text: read(opener) };
    })() : null,
    models: Array.from(new Set([...exactMatches, ...buttonMatches])),
  };
})()
`;
}

async function click(cdp, point) {
  if (!point) return false;
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y, button: 'none' });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 });
  return true;
}

async function evaluate(cdp) {
  const result = await cdp.send('Runtime.evaluate', {
    expression: extractExpression(),
    awaitPromise: false,
    returnByValue: true,
    userGesture: true,
  });
  return (result.result && result.result.value) || {};
}

async function main() {
  const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  const target = chooseTarget(targets);
  if (!target) throw new Error('no Antigravity workbench target found');
  const cdp = await connectCdp(target.webSocketDebuggerUrl);
  try {
    await cdp.send('Runtime.enable');
    await sleep(250);
    let snapshot = await evaluate(cdp);
    if (snapshot.opener) {
      await click(cdp, snapshot.opener);
      await sleep(500);
      snapshot = await evaluate(cdp);
    }
    console.log(JSON.stringify({
      ok: true,
      current: snapshot.current || '',
      models: snapshot.models || [],
    }));
  } finally {
    cdp.close();
  }
}

main().catch(error => {
  console.log(JSON.stringify({ ok: false, reason: error.message || String(error), models: [] }));
  process.exitCode = 1;
});
