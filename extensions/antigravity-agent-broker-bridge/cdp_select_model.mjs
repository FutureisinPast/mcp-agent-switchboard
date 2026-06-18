#!/usr/bin/env node

const argv = process.argv.slice(2);

function arg(name, fallback = '') {
  const idx = argv.indexOf(`--${name}`);
  if (idx >= 0 && idx + 1 < argv.length) {
    const value = argv[idx + 1];
    return value && value.startsWith('--') ? fallback : value;
  }
  return fallback;
}

const targetModel = arg('model', argv[0] || '');
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
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
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
    if (haystack.includes('vscode')) score += 20;
    if (haystack.includes('chat') || haystack.includes('agent')) score += 10;
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
    const contexts = [];
    const timer = setTimeout(() => {
      try { ws.close(); } catch {}
      reject(new Error('CDP websocket connection timed out'));
    }, timeoutMs);

    ws.onopen = () => {
      clearTimeout(timer);
      resolve({
        contexts,
        close: () => ws.close(),
        send(method, params = {}) {
          const id = nextId++;
          const payload = JSON.stringify({ id, method, params });
          return new Promise((innerResolve, innerReject) => {
            pending.set(id, { resolve: innerResolve, reject: innerReject });
            ws.send(payload);
          });
        },
      });
    };

    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error('CDP websocket error'));
    };

    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.executionContextCreated') {
        contexts.push(message.params.context);
      }
      if (!message.id || !pending.has(message.id)) {
        return;
      }
      const item = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        item.reject(new Error(message.error.message || JSON.stringify(message.error)));
      } else {
        item.resolve(message.result || {});
      }
    };
  });
}

function findExpression(model, phase) {
  return `
(() => {
  const target = ${JSON.stringify(model)};
  const phase = ${JSON.stringify(phase)};
  const normalize = value => String(value || '')
    .toLowerCase()
    .replace(/[()\\[\\]{}]/g, ' ')
    .replace(/[^a-z0-9.]+/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim();
  const compact = value => normalize(value).replace(/[^a-z0-9.]+/g, '');
  const targetNorm = normalize(target);
  const targetCompact = compact(target);
  const targetTokens = targetNorm.split(' ').filter(Boolean);
  const targetTokenCompacts = targetTokens.map(compact).filter(token => token.length > 0);
  const roots = [];
  const seenRoots = new Set();
  const collectRoot = root => {
    if (!root || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    const items = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
    for (const item of items) {
      if (item.shadowRoot) collectRoot(item.shadowRoot);
    }
  };
  collectRoot(document);

  const read = el => [
    el.innerText,
    el.textContent,
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('title'),
    el.value,
  ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();

  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 2 && rect.height > 2 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      Number(style.opacity || '1') > 0;
  };

  const clickable = el => {
    if (!el || !el.matches) return false;
    const role = el.getAttribute('role') || '';
    return el.matches('button, a, input, [aria-haspopup], [aria-expanded], .monaco-button') ||
      /button|option|menuitem|listitem|combobox/.test(role);
  };

  const clickableAncestor = el => {
    let current = el;
    for (let i = 0; current && i < 6; i += 1) {
      if (clickable(current)) return current;
      current = current.parentElement;
    }
    return el;
  };

  const isModelText = text => /\\b(gemini|claude|gpt|sonnet|opus|flash|pro|thinking|oss)\\b/i.test(text);
  const matchesTarget = text => {
    const textCompact = compact(text);
    if (!targetCompact || !textCompact) return false;
    if (textCompact === targetCompact || textCompact.includes(targetCompact)) return true;
    return targetTokenCompacts.length > 0 &&
      targetTokenCompacts.every(token => textCompact.includes(token));
  };

  const candidates = [];
  for (const root of roots) {
    const selector = 'button, a, input, li, [role], [aria-label], [title], div, span';
    for (const raw of Array.from(root.querySelectorAll(selector))) {
      if (!visible(raw)) continue;
      const el = clickableAncestor(raw);
      if (!visible(el)) continue;
      const text = read(el) || read(raw);
      if (!text) continue;
      let score = 0;
      if (phase === 'target') {
        if (!clickable(el)) continue;
        if (!isModelText(text)) continue;
        if (text.length > 140) continue;
        if (!matchesTarget(text)) continue;
        score += 1000;
        score += 100;
        if (/option|menuitem|listitem/.test(el.getAttribute('role') || '')) score += 80;
        if (normalize(text) === targetNorm) score += 200;
        if (text.length < 80) score += 40;
        score -= Math.min(text.length, 300);
      } else {
        if (!isModelText(text)) continue;
        score += 300;
        if (clickable(el)) score += 120;
        if (el.getAttribute('aria-haspopup')) score += 100;
        if (el.getAttribute('aria-expanded') !== null) score += 80;
        if (text.length < 80) score += 60;
        if (matchesTarget(text)) score += 30;
        score -= Math.min(text.length, 200);
      }
      const rect = el.getBoundingClientRect();
      candidates.push({
        text,
        score,
        x: Math.round(rect.left + rect.width / 2),
        y: Math.round(rect.top + rect.height / 2),
      });
    }
  }
  candidates.sort((a, b) => b.score - a.score);
  return {
    ok: candidates.length > 0,
    phase,
    target,
    candidate: candidates[0] || null,
    candidates: candidates.slice(0, 8).map(item => ({ text: item.text, score: item.score })),
  };
})()
`;
}

function snapshotExpression(model) {
  return `
(() => {
  const target = ${JSON.stringify(model)};
  const normalize = value => String(value || '')
    .replace(/\\s+/g, ' ')
    .trim();
  const compact = value => normalize(value)
    .toLowerCase()
    .replace(/[()\\[\\]{}]/g, ' ')
    .replace(/[^a-z0-9.]+/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim()
    .replace(/[^a-z0-9.]+/g, '');
  const targetNorm = normalize(target);
  const targetCompact = compact(target);
  const targetTokens = targetNorm
    .toLowerCase()
    .replace(/[()\\[\\]{}]/g, ' ')
    .replace(/[^a-z0-9.]+/g, ' ')
    .replace(/\\s+/g, ' ')
    .trim()
    .split(' ')
    .map(compact)
    .filter(Boolean);
  const matchesTarget = text => {
    const textCompact = compact(text);
    if (!targetCompact || !textCompact) return false;
    if (textCompact === targetCompact || textCompact.includes(targetCompact)) return true;
    return targetTokens.length > 0 && targetTokens.every(token => textCompact.includes(token));
  };
  const visible = el => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 2 && rect.height > 2 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      Number(style.opacity || '1') > 0;
  };
  const read = el => normalize([
    el.innerText,
    el.textContent,
    el.getAttribute && el.getAttribute('aria-label'),
    el.getAttribute && el.getAttribute('title'),
  ].filter(Boolean).join(' '));
  const roots = [];
  const seenRoots = new Set();
  const collectRoot = root => {
    if (!root || seenRoots.has(root)) return;
    seenRoots.add(root);
    roots.push(root);
    const items = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
    for (const item of items) {
      if (item.shadowRoot) collectRoot(item.shadowRoot);
    }
  };
  collectRoot(document);
  const exactModelPattern = /(Gemini\\s+[0-9.]+\\s+(?:Flash|Pro)\\s+\\([^)]+\\)|Claude\\s+(?:Sonnet|Opus|Haiku)\\s+[0-9.]+\\s+\\([^)]+\\)|GPT-OSS\\s+[0-9A-Z]+\\s+\\([^)]+\\))/g;
  let opener = null;
  let current = '';
  const models = [];
  for (const root of roots) {
    const controls = Array.from(root.querySelectorAll('button,[role="button"],[aria-haspopup],[aria-expanded]')).filter(visible);
    for (const control of controls) {
      const text = read(control);
      if (!text) continue;
      const matches = Array.from(text.matchAll(exactModelPattern)).map(match => normalize(match[1] || match[0]));
      models.push(...matches);
      if (!opener && (/select model/i.test(text) || /\\b(gemini|claude|gpt|sonnet|opus|flash|pro|thinking|oss)\\b/i.test(text))) {
        const rect = control.getBoundingClientRect();
        opener = {
          text,
          x: Math.round(rect.left + rect.width / 2),
          y: Math.round(rect.top + rect.height / 2),
        };
      }
      if (/select model/i.test(text) || matches.length) {
        const currentMatch = text.match(/current:\\s*([^\\n]+)/i);
        if (currentMatch) {
          current = normalize(currentMatch[1]);
        } else if (!current && matches.length === 1 && text.length < 120) {
          current = matches[0];
        }
      }
    }
    const bodyText = normalize(root.body ? root.body.innerText : root.textContent || '');
    models.push(...Array.from(bodyText.matchAll(exactModelPattern)).map(match => normalize(match[1] || match[0])));
  }
  return {
    ok: true,
    target,
    current,
    currentMatches: matchesTarget(current),
    opener,
    models: Array.from(new Set(models)).slice(0, 20),
  };
})()
`;
}

async function evaluateFind(cdp, contexts, phase) {
  const expression = findExpression(targetModel, phase);
  const contextIds = [...new Set(contexts.map(item => item.id).filter(Boolean))];
  const attempts = [undefined, ...contextIds];
  let best = null;
  for (const contextId of attempts) {
    try {
      const result = await cdp.send('Runtime.evaluate', {
        expression,
        awaitPromise: false,
        returnByValue: true,
        userGesture: true,
        ...(contextId ? { contextId } : {}),
      });
      const value = result.result && result.result.value;
      if (value && value.ok && (!best || value.candidate.score > best.candidate.score)) {
        best = value;
      }
    } catch {
      // Ignore contexts that cannot access a DOM.
    }
  }
  return best || { ok: false, phase, target: targetModel, candidate: null, candidates: [] };
}

async function evaluateSnapshot(cdp, contexts) {
  const expression = snapshotExpression(targetModel);
  const contextIds = [...new Set(contexts.map(item => item.id).filter(Boolean))];
  const attempts = [undefined, ...contextIds];
  let best = null;
  for (const contextId of attempts) {
    try {
      const result = await cdp.send('Runtime.evaluate', {
        expression,
        awaitPromise: false,
        returnByValue: true,
        userGesture: true,
        ...(contextId ? { contextId } : {}),
      });
      const value = result.result && result.result.value;
      if (value && value.ok && (!best || (value.currentMatches && !best.currentMatches) || ((value.models || []).length > (best.models || []).length))) {
        best = value;
      }
    } catch {
      // Ignore contexts that cannot access a DOM.
    }
  }
  return best || { ok: false, target: targetModel, current: '', currentMatches: false, opener: null, models: [] };
}

async function clickCandidate(cdp, candidate) {
  if (!candidate || typeof candidate.x !== 'number' || typeof candidate.y !== 'number') {
    return false;
  }
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved',
    x: candidate.x,
    y: candidate.y,
    button: 'none',
  });
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mousePressed',
    x: candidate.x,
    y: candidate.y,
    button: 'left',
    clickCount: 1,
  });
  await cdp.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased',
    x: candidate.x,
    y: candidate.y,
    button: 'left',
    clickCount: 1,
  });
  return true;
}

async function main() {
  if (!targetModel) {
    throw new Error('missing --model');
  }
  const targets = await fetchJson(`http://127.0.0.1:${port}/json/list`);
  const target = chooseTarget(targets);
  if (!target) {
    throw new Error('no debuggable Antigravity page target found');
  }
  const cdp = await connectCdp(target.webSocketDebuggerUrl);
  try {
    await cdp.send('Runtime.enable');
    await sleep(250);

    const before = await evaluateSnapshot(cdp, cdp.contexts);
    if (before.currentMatches) {
      console.log(JSON.stringify({
        ok: true,
        mode: 'already-selected',
        targetModel,
        current: before.current,
        verified: true,
      }));
      return;
    }

    const direct = await evaluateFind(cdp, cdp.contexts, 'target');
    if (direct.ok) {
      await clickCandidate(cdp, direct.candidate);
      await sleep(800);
      const afterDirect = await evaluateSnapshot(cdp, cdp.contexts);
      if (afterDirect.currentMatches) {
        console.log(JSON.stringify({
          ok: true,
          mode: 'target-visible',
          targetModel,
          clicked: direct.candidate.text,
          current: afterDirect.current,
          verified: true,
        }));
        return;
      }
    }

    const snapshot = await evaluateSnapshot(cdp, cdp.contexts);
    const opener = snapshot.opener || (await evaluateFind(cdp, cdp.contexts, 'opener')).candidate;
    if (!opener) {
      console.log(JSON.stringify({
        ok: false,
        reason: 'model selector opener not found',
        targetModel,
        current: snapshot.current || before.current || '',
        models: snapshot.models || before.models || [],
      }));
      process.exitCode = 2;
      return;
    }

    await clickCandidate(cdp, opener);
    await sleep(650);

    const selected = await evaluateFind(cdp, cdp.contexts, 'target');
    if (!selected.ok) {
      console.log(JSON.stringify({
        ok: false,
        reason: 'target model option not found after opening selector',
        targetModel,
        current: snapshot.current || before.current || '',
        opener: opener.text,
        models: snapshot.models || before.models || [],
        candidates: selected.candidates,
      }));
      process.exitCode = 3;
      return;
    }

    await clickCandidate(cdp, selected.candidate);
    await sleep(800);
    const after = await evaluateSnapshot(cdp, cdp.contexts);
    if (!after.currentMatches) {
      console.log(JSON.stringify({
        ok: false,
        reason: 'target model click did not verify as selected',
        targetModel,
        current: after.current || '',
        opener: opener.text,
        clicked: selected.candidate.text,
        models: after.models || snapshot.models || before.models || [],
      }));
      process.exitCode = 4;
      return;
    }
    console.log(JSON.stringify({
      ok: true,
      mode: 'opened-selector',
      targetModel,
      current: after.current,
      opener: opener.text,
      clicked: selected.candidate.text,
      verified: true,
    }));
  } finally {
    cdp.close();
  }
}

main().catch(error => {
  console.log(JSON.stringify({ ok: false, reason: error.message || String(error), targetModel }));
  process.exitCode = 1;
});
