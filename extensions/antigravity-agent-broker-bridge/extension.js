const cp = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vscode = require('vscode');

let output;
let timer;
let busy = false;
let statusBar;
let extensionContext;
const sentIds = new Set();
const notifiedCodexIds = new Set();
const notifiedCompletionIds = new Set();
const injectedClaudeFiles = new Set();
let claudeFirstPoll = true;
let antigravitySendSupported = null;
let antigravitySendCheckedAt = 0;
const ANTIGRAVITY_SEND_RECHECK_MS = 15000;
const extensionStartedAt = Date.now();

const taskContracts = {
  quick_check: [
    'Return the answer in at most 8 bullets.',
    'Do not read broad files or restate the context pack unless needed.',
    'Flag uncertainty instead of expanding scope.',
  ],
  implementation_plan: [
    'Produce an exact implementation plan, not code edits.',
    'Use numbered steps with target files, functions, required checks, and rollback/risks.',
    'Do not invent architecture beyond the request.',
    'Make the plan deterministic: include acceptance criteria and forbidden changes.',
    'Do not continue if critical context is missing; ask one concise blocking question.',
  ],
  implementation: [
    'Implement only the requested change.',
    'Follow the approved plan and acceptance criteria as binding constraints.',
    'Do not redesign, reorder, expand scope, or substitute architecture.',
    'Do not refactor unrelated code or change behavior outside scope.',
    'If any plan step is impossible or ambiguous, stop and report the blocker instead of improvising.',
    'Report files changed, checks run, and remaining risks.',
  ],
  co_audit: [
    'Audit for bugs, missed edge cases, bad assumptions, and missing tests.',
    'Findings first, ordered by severity, with evidence.',
    'Do not rewrite the solution unless asked.',
    'Keep the audit bounded to the provided topic/context.',
  ],
  debate: [
    'Argue the strongest technical case for and against the proposal.',
    'Separate facts, assumptions, and opinions.',
    'End with a concrete recommendation and confidence.',
    'Do not spend tokens restating areas where agents already agree.',
  ],
  argue: [
    'Challenge the proposal directly and look for failure modes.',
    'Do not be agreeable for its own sake.',
    'End with what would change your mind.',
  ],
  review: [
    'Use code-review style: bugs, regressions, missing tests, and risks first.',
    'Cite exact files/lines when available.',
    'Avoid summaries unless there are no issues.',
  ],
  bug_hunt: [
    'Focus on reproducing, isolating, and explaining the bug.',
    'List likely root causes with evidence and next diagnostic command.',
    'Do not propose broad rewrites.',
  ],
  sanity_check: [
    'Check whether the plan/request is coherent and safe.',
    'Return pass/fail/concerns with minimal explanation.',
    'Do not expand into implementation.',
  ],
  consult: [
    'Answer the exact question.',
    'Keep context usage low: use the context pack first, then expand only specific evidence.',
    'State assumptions and concrete next action.',
  ],
};

function config() {
  const cfg = vscode.workspace.getConfiguration('agentBrokerBridge');
  const defaultBrokerPath = path.join(os.homedir(), '.agent-broker', 'agent_broker_mcp.py');
  return {
    enabled: cfg.get('enabled', true),
    pollIntervalMs: cfg.get('pollIntervalMs', 3000),
    pythonPath: cfg.get('pythonPath', 'python'),
    brokerPath: cfg.get('brokerPath', '') || defaultBrokerPath,
    showCompletionNotifications: cfg.get('showCompletionNotifications', false),
    showCodexInboxNotifications: cfg.get('showCodexInboxNotifications', true),
    autoOpenCodexInbox: cfg.get('autoOpenCodexInbox', true),
    autoSendCodexInboxToChat: cfg.get('autoSendCodexInboxToChat', true),
    autoOpenCodexSidebar: cfg.get('autoOpenCodexSidebar', true),
    codexSidebarCommands: cfg.get('codexSidebarCommands', [
      'chatgpt.openSidebar',
      'openai.chatgpt.openSidebar',
      'codex.openSidebar',
      'workbench.view.extension.chatgpt',
      'workbench.view.extension.openai-chatgpt',
    ]),
    confirmStrictModelRequests: cfg.get('confirmStrictModelRequests', true),
    autoSelectStrictModel: cfg.get('autoSelectStrictModel', true),
    useCdpModelSelection: cfg.get('useCdpModelSelection', false),
    cdpPort: configuredCdpPort(cfg),
    cdpSelectTimeoutMs: cfg.get('cdpSelectTimeoutMs', 5000),
    cdpSelectorPath: cfg.get('cdpSelectorPath', ''),
    nodePath: cfg.get('nodePath', 'node'),
    strictModelAutoSendDelayMs: cfg.get('strictModelAutoSendDelayMs', 1500),
    autoOpenStrictModelSelector: cfg.get('autoOpenStrictModelSelector', true),
    strictModelAutoResumeDelayMs: cfg.get('strictModelAutoResumeDelayMs', 0),
    antigravityAutoSubmit: cfg.get('antigravityAutoSubmit', true),
    antigravityAutoSubmitDelayMs: cfg.get('antigravityAutoSubmitDelayMs', 1800),
    claudeUseTopicSession: cfg.get('claudeUseTopicSession', true),
    claudeAutoSubmit: cfg.get('claudeAutoSubmit', true),
    claudeAutoSubmitDelayMs: cfg.get('claudeAutoSubmitDelayMs', 1200),
    claudeInboxStartupMaxAgeMs: cfg.get('claudeInboxStartupMaxAgeMs', 10 * 60 * 1000),
    snapshotPolling: cfg.get('snapshotPolling', true),
    snapshotConsumer: cfg.get('snapshotConsumer', 'snapshot-bridge'),
    snapshotClaudeCapable: cfg.get('snapshotClaudeCapable', true),
  };
}

function log(message) {
  output.appendLine(`${new Date().toISOString()} ${message}`);
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function defaultCdpPort() {
  const appName = `${vscode.env.appName || ''} ${vscode.env.uriScheme || ''}`.toLowerCase();
  return appName.includes('code') && !appName.includes('antigravity') ? 9010 : 9000;
}

function configuredCdpPort(cfg) {
  const inspected = cfg.inspect('cdpPort');
  const userConfigured = inspected && (
    inspected.globalValue !== undefined ||
    inspected.workspaceValue !== undefined ||
    inspected.workspaceFolderValue !== undefined
  );
  return userConfigured ? cfg.get('cdpPort', defaultCdpPort()) : defaultCdpPort();
}

function runBroker(args) {
  const cfg = config();
  return new Promise((resolve, reject) => {
    const child = cp.spawn(cfg.pythonPath, [cfg.brokerPath, 'bridge', ...args], {
      windowsHide: true,
      cwd: os.homedir(),
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk.toString(); });
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) {
        reject(new Error(`broker exited ${code}: ${stderr || stdout}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout || '{}'));
      } catch (err) {
        reject(new Error(`invalid broker JSON: ${stdout}`));
      }
    });
  });
}

function responsePath(requestOrId) {
  const request = typeof requestOrId === 'object' ? requestOrId : null;
  const requestId = request ? request.id : requestOrId;
  const base = request && request.root_path ? request.root_path : os.homedir();
  const dir = request && request.root_path
    ? path.join(base, '.agent-broker', 'antigravity-responses')
    : path.join(base, '.agent-broker', 'antigravity-responses');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, `${requestId}.md`);
}

function legacyResponsePath(requestId) {
  const dir = path.join(os.homedir(), '.agent-broker', 'antigravity-responses');
  return path.join(dir, `${requestId}.md`);
}

function responsePaths(request) {
  const paths = [responsePath(request), legacyResponsePath(request.id)];
  return [...new Set(paths)];
}

async function getContextPack(project, topic, budget = 8000) {
  try {
    const result = await runBroker(['context-pack', project || '*', topic || '*', String(budget)]);
    return result.content || '';
  } catch (err) {
    log(`context pack failed: ${err.message || err}`);
    return '';
  }
}

function codexInboxPath(requestId) {
  const dir = path.join(os.homedir(), '.agent-broker', 'codex-inbox');
  fs.mkdirSync(dir, { recursive: true });
  return path.join(dir, `${requestId}.md`);
}

function taskContract(request) {
  const kind = request.task_kind || request.request_type || 'consult';
  const items = taskContracts[kind] || taskContracts.consult;
  const budget = request.token_budget || 2500;
  return [
    `Task kind: ${kind}`,
    `Response budget: about ${budget} words or less unless explicitly required.`,
    'Ground rules:',
    ...items.map(item => `- ${item}`),
    '- Use the shared context pack first.',
    '- Expand only specific files/history/events that are needed.',
    '- If the context pack includes context_ref markers, retrieve only the specific ref/query you need instead of asking for broad history.',
    '- For vague model requests, call resolve_model_request first. If it returns needs_model_selection, ask the user to choose from the returned choices and remember that choice with set_model_default.',
    '- Reuse the remembered model default for this topic until the user explicitly asks to change it.',
    '- Do not repeat full context back to the caller.',
    '- Record important findings as context events when tools are available.',
  ].join('\n');
}

function modelSearchText(targetModel) {
  const text = String(targetModel || '').trim();
  if (/gemini/i.test(text) && /flash/i.test(text)) {
    return 'Gemini 3.5 Flash';
  }
  if (/opus/i.test(text)) {
    return 'Claude Opus 4.6';
  }
  if (/sonnet/i.test(text)) {
    return 'Claude Sonnet 4.6';
  }
  return text;
}

async function tryCommand(command, args) {
  try {
    await vscode.commands.executeCommand(command, ...(args || []));
    return true;
  } catch (err) {
    log(`${command} failed: ${err.message || err}`);
    return false;
  }
}

function runNodeJson(args, timeoutMs) {
  const cfg = config();
  return new Promise((resolve) => {
    const child = cp.spawn(cfg.nodePath, args, {
      windowsHide: true,
      cwd: os.homedir(),
    });
    let stdout = '';
    let stderr = '';
    const timerId = setTimeout(() => {
      try {
        child.kill();
      } catch {}
      resolve({ ok: false, reason: `timed out after ${timeoutMs}ms` });
    }, timeoutMs + 1000);
    child.stdout.on('data', chunk => { stdout += chunk.toString(); });
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.on('error', err => {
      clearTimeout(timerId);
      resolve({ ok: false, reason: err.message || String(err) });
    });
    child.on('close', code => {
      clearTimeout(timerId);
      const text = stdout.trim();
      try {
        const parsed = JSON.parse(text || '{}');
        parsed.exitCode = code;
        if (!parsed.reason && stderr.trim()) {
          parsed.stderr = stderr.trim();
        }
        resolve(parsed);
      } catch (err) {
        resolve({
          ok: false,
          reason: `invalid JSON from node helper: ${err.message || err}`,
          stdout: text,
          stderr: stderr.trim(),
          exitCode: code,
        });
      }
    });
  });
}

async function attemptCdpModelSelection(targetModel) {
  const cfg = config();
  if (!cfg.useCdpModelSelection) {
    return { attempted: false, reason: 'disabled' };
  }
  const script = cfg.cdpSelectorPath
    || path.join(extensionContext ? extensionContext.extensionPath : __dirname, 'cdp_select_model.mjs');
  if (!fs.existsSync(script)) {
    return { attempted: false, reason: `missing CDP selector script: ${script}` };
  }
  const result = await runNodeJson([
    script,
    '--model', targetModel,
    '--port', String(cfg.cdpPort),
    '--timeout', String(cfg.cdpSelectTimeoutMs),
  ], cfg.cdpSelectTimeoutMs);
  return { attempted: true, cdp: true, port: cfg.cdpPort, ...result };
}

async function attemptSelectAntigravityModel(targetModel) {
  const search = modelSearchText(targetModel);
  log(`Attempting Antigravity model selection: ${targetModel}`);
  await tryCommand('antigravity.agentSidePanel.open');
  await delay(250);
  const cdpResult = await attemptCdpModelSelection(targetModel);
  if (cdpResult.attempted) {
    log(`CDP model selection result for ${targetModel}: ${JSON.stringify(cdpResult)}`);
    if (cdpResult.ok) {
      return { attempted: true, method: 'cdp', search, ...cdpResult };
    }
  }
  await tryCommand('antigravity.toggleModelSelector');
  await delay(500);
  await tryCommand('type', [{ text: search }]);
  await delay(350);
  const accepted = await tryCommand('workbench.action.acceptSelectedQuickOpenItem')
    || await tryCommand('list.select')
    || await tryCommand('acceptSelectedSuggestion');
  await delay(500);
  return { attempted: true, accepted, search };
}

function buildPrompt(request) {
  const fallbackPath = responsePath(request);
  const contextPack = request.context_pack || '';
  const strictModel = Number(request.strict_model || 0) === 1;
  return [
    '# Agent Broker Request',
    '',
    `Request ID: ${request.id}`,
    `Project: ${request.project}`,
    `Project path: ${request.root_path || ''}`,
    `Topic: ${request.topic || 'default'}`,
    `Requested target model: ${request.target_model || 'current Antigravity selected model'}`,
    `Request type: ${request.request_type || 'consult'}`,
    `Task kind: ${request.task_kind || request.request_type || 'consult'}`,
    `Strict target model: ${strictModel ? 'yes' : 'no'}`,
    `Token budget: ${request.token_budget || 2500}`,
    '',
    'You are responding from inside Antigravity using the currently selected in-app model and the user subscription. Keep this on the same topic and use the project context when needed.',
    '',
    strictModel
      ? 'First confirm the visible selected model name. If it does not match the requested target model, STOP. Do not perform the task under the wrong model. Write a short mismatch response to the fallback file and include a Codex Callback asking Codex to requeue after the user selects the target model.'
      : 'First confirm the visible selected model name. If it does not match the requested target model, state the mismatch clearly before answering.',
    '',
    'Use the shared context pack before broad file reads or broad history reads. Expand only specific files, events, or history entries when needed.',
    'If the pack includes `context_ref=ctx_...`, use MCP tool `retrieve_shared_context` with a narrow query when available. If MCP tools are unavailable, include a Codex Callback asking Codex to retrieve the exact ref/query.',
    'If the user asks for a model family rather than a concrete model, such as "ask Codex", "take GPT side", "ask Claude", or "ask Opus", use `resolve_model_request`/`list_agent_models` first. Ask the user to choose once, then call `set_model_default` so this topic keeps that model until the user changes it.',
    '',
    '## Task Contract',
    '',
    taskContract(request),
    '',
    '## Shared Context Pack',
    '',
    contextPack || 'No context pack is available yet.',
    '',
    'When finished, complete the bridge request in ONE of these ways:',
    '',
    '1. Preferred: call MCP tool `complete_antigravity_request` with:',
    `   - request_id: ${request.id}`,
    '   - response: your final answer',
    '   - model: the selected Antigravity model name if visible',
    '',
    '2. Fallback: write your final answer to this file:',
    `   ${fallbackPath}`,
    '',
    'If you need Codex to respond and MCP tools are unavailable, include this section at the end of that same fallback file:',
    '',
    '## Codex Callback',
    '<the exact prompt Codex should answer>',
    '',
    'Do not write directly to the Codex inbox; the bridge will queue the callback.',
    '',
    'Also call `record_agent_event` for any important findings, searches, files inspected, or decisions.',
    '',
    'Original prompt:',
    '',
    request.prompt || '',
  ].join('\n');
}

function pressEnterInAntigravityWindow(timeoutMs = 5000) {
  const script = `
$names = @('Antigravity IDE','Antigravity')
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class BrokerAgKeys {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
'@
$p = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { ($names -contains $_.ProcessName) -and ($_.MainWindowHandle -ne 0) } |
  Select-Object -First 1
if (-not $p) { 'no-window'; exit 2 }
[BrokerAgKeys]::ShowWindow($p.MainWindowHandle, 9) | Out-Null
[BrokerAgKeys]::SetForegroundWindow($p.MainWindowHandle) | Out-Null
Start-Sleep -Milliseconds 180
[BrokerAgKeys]::keybd_event(0x0D, 0, 0, [UIntPtr]::Zero)
Start-Sleep -Milliseconds 60
[BrokerAgKeys]::keybd_event(0x0D, 0, 2, [UIntPtr]::Zero)
'pressed'
`.trim();
  return new Promise(resolve => {
    const child = cp.spawn('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], {
      windowsHide: true,
      cwd: os.homedir(),
    });
    let stdout = '';
    let stderr = '';
    const timerId = setTimeout(() => {
      try {
        child.kill();
      } catch {}
      resolve({ ok: false, method: 'enter-key', reason: `timed out after ${timeoutMs}ms` });
    }, timeoutMs);
    child.stdout.on('data', chunk => { stdout += chunk.toString(); });
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.on('error', err => {
      clearTimeout(timerId);
      resolve({ ok: false, method: 'enter-key', reason: err.message || String(err) });
    });
    child.on('close', code => {
      clearTimeout(timerId);
      const detail = (stdout.trim().split(/\r?\n/).filter(Boolean).pop() || '').trim();
      resolve({ ok: code === 0 && detail === 'pressed', method: 'enter-key', detail, error: stderr.trim() || undefined });
    });
  });
}

async function submitAntigravityDraft() {
  const cfg = config();
  if (!cfg.antigravityAutoSubmit) {
    return { attempted: false, ok: false, reason: 'disabled' };
  }
  await delay(Math.max(0, cfg.antigravityAutoSubmitDelayMs));
  const submit = await pressEnterInAntigravityWindow();
  if (submit.ok) {
    return { attempted: true, ok: true, method: submit.method, detail: submit.detail };
  }
  return { attempted: true, ok: false, method: submit.method, reason: submit.reason || submit.detail || submit.error || 'failed' };
}

async function sendToAntigravity(request) {
  if (sentIds.has(request.id)) {
    return;
  }
  sentIds.add(request.id);
  // Truncate any pre-existing (stale) fallback file so a leftover response from an
  // earlier attempt can't immediately re-complete this freshly (re)sent request.
  const fallbackPath = responsePath(request);
  fs.writeFileSync(fallbackPath, '', 'utf8');
  const cfg = config();
  const strictModel = Number(request.strict_model || 0) === 1;
  const targetModel = request.target_model || 'Antigravity current selected model';
  if (strictModel && cfg.confirmStrictModelRequests) {
    if (cfg.autoSelectStrictModel) {
      const result = await attemptSelectAntigravityModel(targetModel);
      log(`Model selection attempt for ${targetModel}: ${JSON.stringify(result)}`);
      if (!result.ok) {
        await runBroker(['await-model', request.id]);
        updateStatus('$(warning) Broker: select model');
        await vscode.window.showWarningMessage(
          `Could not verify Antigravity selected ${targetModel}. Select it manually, then run Agent Broker Bridge: Poll Now.`,
          'OK'
        );
        sentIds.delete(request.id);
        return;
      }
      await delay(Math.max(0, cfg.strictModelAutoSendDelayMs));
      const prompt = buildPrompt(request);
      log(`Sending strict request ${request.id} after model selection attempt`);
      try {
        await vscode.commands.executeCommand('antigravity.agentSidePanel.open');
      } catch (err) {
        log(`agentSidePanel.open failed: ${err.message || err}`);
      }
      try {
        await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', prompt);
      } catch (err) {
        log(`strict sendPromptToAgentPanel failed for ${request.id}; requeueing: ${err.message || err}`);
        await runBroker(['requeue', request.id]);
        sentIds.delete(request.id);
        updateStatus('$(warning) Broker: send failed');
        return;
      }
      const submit = await submitAntigravityDraft();
      log(`Strict Antigravity request ${request.id} submit=${JSON.stringify(submit)}`);
      return;
    }
    if (cfg.autoOpenStrictModelSelector) {
      await tryCommand('antigravity.agentSidePanel.open');
      await delay(250);
      await tryCommand('antigravity.toggleModelSelector');
      await runBroker(['await-model', request.id]);
      updateStatus('$(warning) Broker: select model');
      if (cfg.strictModelAutoResumeDelayMs > 0) {
        setTimeout(async () => {
          try {
            await runBroker(['resume-model', request.id]);
            pollOnce();
          } catch (err) {
            log(`auto resume failed: ${err.message || err}`);
          }
        }, cfg.strictModelAutoResumeDelayMs);
      }
      await vscode.window.showInformationMessage(`Select ${targetModel}. Then run Agent Broker Bridge: Poll Now.`, 'OK');
      sentIds.delete(request.id);
      return;
    }
    const action = await vscode.window.showWarningMessage(
      `Agent Broker target model: ${targetModel}. Select it in Antigravity before sending.`,
      'Open Model Selector',
      'Send Now'
    );
    if (action === 'Open Model Selector') {
      try {
        await vscode.commands.executeCommand('antigravity.toggleModelSelector');
      } catch (err) {
        log(`toggleModelSelector failed: ${err.message || err}`);
      }
      await vscode.window.showInformationMessage(`After selecting ${targetModel}, run Agent Broker Bridge: Poll Now or wait for the next poll.`, 'OK');
      await runBroker(['await-model', request.id]);
      sentIds.delete(request.id);
      return;
    }
    if (action !== 'Send Now') {
      await runBroker(['requeue', request.id]);
      sentIds.delete(request.id);
      return;
    }
  }
  const prompt = buildPrompt(request);
  log(`Sending request ${request.id} to Antigravity agent panel`);
  try {
    await vscode.commands.executeCommand('antigravity.agentSidePanel.open');
  } catch (err) {
    log(`agentSidePanel.open failed: ${err.message || err}`);
  }
  try {
    await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', prompt);
  } catch (err) {
    log(`sendPromptToAgentPanel failed for ${request.id}; requeueing: ${err.message || err}`);
    await runBroker(['requeue', request.id]);
    sentIds.delete(request.id);
    updateStatus('$(warning) Broker: send failed');
    return;
  }
  const submit = await submitAntigravityDraft();
  log(`Antigravity request ${request.id} submit=${JSON.stringify(submit)}`);
}

function claudeInboxDir() {
  const dir = path.join(os.homedir(), '.agent-broker', 'claude-inbox');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function extractClaudeInboxPrompt(content) {
  // queue_claude_request writes a header, then a blockquote, then `---`, then the prompt.
  const marker = content.indexOf('\n---\n');
  const body = marker >= 0 ? content.slice(marker + 5) : content;
  return body.trim();
}

function extractClaudeRequestId(content, fallbackName) {
  const match = content.match(/^Request ID:\s*([0-9a-f-]{20,})\s*$/im);
  if (match) {
    return match[1].trim();
  }
  const base = path.basename(String(fallbackName || ''), '.md');
  return /^[0-9a-f-]{20,}$/i.test(base) ? base : undefined;
}

function extractClaudeHeader(content, name) {
  const value = (label) => {
    const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = content.match(new RegExp(`^${escaped}:\\s*(.+?)\\s*$`, 'im'));
    return match ? match[1].trim() : '';
  };
  return {
    requestId: extractClaudeRequestId(content, name),
    project: value('Project'),
    topic: value('Topic'),
    requestedModel: value('Requested model'),
    newChat: value('New chat'),
    threadPolicy: value('Thread policy'),
    createdBy: value('Created by'),
  };
}

function stableUuid(text) {
  const hex = crypto.createHash('sha256').update(String(text || 'agent-broker')).digest('hex');
  const variant = (parseInt(hex.slice(16, 18), 16) & 0x3f | 0x80).toString(16).padStart(2, '0');
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    `4${hex.slice(13, 16)}`,
    `${variant}${hex.slice(18, 20)}`,
    hex.slice(20, 32),
  ].join('-');
}

function claudeSessionId(meta) {
  const cfg = config();
  if (!cfg.claudeUseTopicSession || String(meta.newChat || '').toLowerCase() === 'yes') {
    return meta.requestId;
  }
  const project = meta.project || currentProjectPath();
  const topic = meta.topic && meta.topic !== '(none)' ? meta.topic : 'default';
  return stableUuid(`claude-topic:${project}:${topic}`);
}

function withClaudeEnvelope(prompt, meta) {
  const requestedModel = meta.requestedModel || 'Claude currently selected model';
  const threadPolicy = meta.threadPolicy || 'same project/topic session by default';
  const lines = [
    `# Agent Broker Routed Request - Requested Claude model: ${requestedModel}`,
    '',
    `Request ID: ${meta.requestId || 'unknown'}`,
    `Project: ${meta.project || 'unknown'}`,
    `Topic: ${meta.topic || 'default'}`,
    `Requested model: ${requestedModel}`,
    `Thread policy: ${threadPolicy}`,
    `Reply target: ${meta.createdBy || 'requesting agent'} through the broker on the same topic unless the user asks for a new chat.`,
    '',
    'Before answering, state the visible/active Claude model if you can see it. If it is not the requested model, report the mismatch first.',
    '',
    prompt.trim(),
  ];
  return lines.join('\n');
}

function hasOpenClaudeTab() {
  try {
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        const label = String(tab.label || '').toLowerCase();
        const viewType = tab.input && tab.input.viewType ? String(tab.input.viewType).toLowerCase() : '';
        if (label.includes('claude') || viewType.includes('claude')) {
          return true;
        }
      }
    }
  } catch (err) {
    log(`claude tab scan failed: ${err.message || err}`);
  }
  return false;
}

// Drive Claude's webview composer over CDP: optionally set the text, then click
// the real Send button (falls back to dispatching Enter). This is the only
// reliable submit path because Claude's composer is a webview, not a VS Code
// chat input, so workbench commands cannot submit it. Prompt goes via a temp
// file to dodge the Windows command-line length limit.
async function runCdpClaudeSend(prompt, setText) {
  const cfg = config();
  if (cfg.useCdpModelSelection === false) {
    return { ok: false, reason: 'cdp disabled' };
  }
  const script = path.join(extensionContext ? extensionContext.extensionPath : __dirname, 'cdp_claude_send.mjs');
  if (!fs.existsSync(script)) {
    return { ok: false, reason: `missing ${script}` };
  }
  const tmp = path.join(os.tmpdir(), `agent-broker-claude-${Date.now()}.txt`);
  let textArgs = ['--text', prompt || ''];
  try {
    fs.writeFileSync(tmp, prompt || '', 'utf8');
    textArgs = ['--text-file', tmp];
  } catch (err) {
    log(`claude temp prompt write failed: ${err.message || err}`);
  }
  const result = await runNodeJson([
    script,
    ...textArgs,
    '--port', String(cfg.cdpPort),
    '--timeout', String(cfg.cdpSelectTimeoutMs),
    '--set-text', String(!!setText),
    '--submit', 'true',
  ], cfg.cdpSelectTimeoutMs);
  try { fs.unlinkSync(tmp); } catch {}
  return result;
}

async function runCdpCodexSend(prompt) {
  const cfg = config();
  if (cfg.useCdpModelSelection === false) {
    return { ok: false, reason: 'cdp disabled' };
  }
  const script = path.join(extensionContext ? extensionContext.extensionPath : __dirname, 'cdp_codex_send.mjs');
  if (!fs.existsSync(script)) {
    return { ok: false, reason: `missing ${script}` };
  }
  const tmp = path.join(os.tmpdir(), `agent-broker-codex-${Date.now()}.txt`);
  let textArgs = ['--text', prompt || ''];
  try {
    fs.writeFileSync(tmp, prompt || '', 'utf8');
    textArgs = ['--text-file', tmp];
  } catch (err) {
    log(`codex temp prompt write failed: ${err.message || err}`);
  }
  const result = await runNodeJson([
    script,
    ...textArgs,
    '--port', String(cfg.cdpPort),
    '--timeout', String(cfg.cdpSelectTimeoutMs),
  ], cfg.cdpSelectTimeoutMs);
  try { fs.unlinkSync(tmp); } catch {}
  return result;
}

async function submitClaudeDraft() {
  const cfg = config();
  if (!cfg.claudeAutoSubmit) {
    return { attempted: false, ok: false, reason: 'disabled' };
  }
  await delay(cfg.claudeAutoSubmitDelayMs);
  // Primary: click Claude's Send button in its webview over CDP.
  const cdp = await runCdpClaudeSend('', false);
  if (cdp && cdp.ok) {
    return { attempted: true, ok: true, method: `cdp:${cdp.method || 'send'}`, cdp };
  }
  log(`Claude CDP submit not ok (${JSON.stringify(cdp)}); trying command fallbacks`);
  await tryCommand('claude-vscode.focus');
  await delay(100);
  if (await tryCommand('workbench.action.chat.submit')) {
    return { attempted: true, ok: true, method: 'workbench.action.chat.submit' };
  }
  const useCtrlEnterToSend = vscode.workspace.getConfiguration('claudeCode').get('useCtrlEnterToSend', false);
  if (useCtrlEnterToSend) {
    return { attempted: true, ok: false, reason: 'Claude is configured to send with Ctrl+Enter; plain Enter would only add a newline' };
  }
  return { attempted: true, ok: false, reason: 'no safe Claude submit path succeeded' };
}

// Inject a prompt into the Claude Code extension. The extension exposes no
// public "send prompt" command, but its OWN code invokes editor.open /
// primaryEditor.open with (sessionId, initialPrompt), so we reuse that with an
// Agent Broker request id as the session id. Deep link and opening the file are
// fallbacks.
async function injectClaudePrompt(prompt, sessionId) {
  let available = new Set();
  try {
    available = new Set(await vscode.commands.getCommands(true));
  } catch (err) {
    log(`getCommands failed: ${err.message || err}`);
  }
  const commandAttempts = [
    ['claude-vscode.primaryEditor.open', [sessionId, prompt]],
    ['claude-vscode.editor.open', [sessionId, prompt]],
  ];
  for (const [command, args] of commandAttempts) {
    if (available.size && !available.has(command)) {
      log(`${command} not registered in this host`);
      continue;
    }
    if (await tryCommand(command, args)) {
      const submit = await submitClaudeDraft();
      log(`Injected Claude prompt via ${command}; submit=${JSON.stringify(submit)}`);
      return { ok: true, method: command, submit };
    }
  }
  // Fallback: the extension's registered URI handler (/open?prompt=).
  for (const scheme of ['vscode', 'antigravity']) {
    try {
      const sessionParam = sessionId ? `session=${encodeURIComponent(sessionId)}&` : '';
      const uri = vscode.Uri.parse(`${scheme}://anthropic.claude-code/open?${sessionParam}prompt=${encodeURIComponent(prompt)}`);
      const opened = await vscode.env.openExternal(uri);
      if (opened) {
        const submit = await submitClaudeDraft();
        log(`Injected Claude prompt via ${scheme}:// deep link; submit=${JSON.stringify(submit)}`);
        return { ok: true, method: `deeplink:${scheme}`, submit };
      }
    } catch (err) {
      log(`claude deep link (${scheme}) failed: ${err.message || err}`);
    }
  }
  return { ok: false };
}

async function pollClaudeInbox() {
  let dir;
  try {
    dir = claudeInboxDir();
  } catch (err) {
    log(`claude inbox dir failed: ${err.message || err}`);
    return;
  }
  let files;
  try {
    files = fs.readdirSync(dir).filter(name => name.toLowerCase().endsWith('.md'));
  } catch (err) {
    log(`claude inbox scan failed: ${err.message || err}`);
    return;
  }
  // On the first poll after activation, baseline old stale files so we do not
  // inject requests that predate this session. Do not baseline fresh files:
  // the broker often writes a Claude request and then launches VS Code, so the
  // request can already exist by the time this extension's first poll runs.
  if (claudeFirstPoll) {
    for (const name of files) {
      const full = path.join(dir, name);
      try {
        const stat = fs.statSync(full);
        const ageMs = Math.max(0, extensionStartedAt - stat.mtimeMs);
        if (ageMs > config().claudeInboxStartupMaxAgeMs) {
          injectedClaudeFiles.add(full);
        }
      } catch (err) {
        log(`claude inbox stat failed for ${name}: ${err.message || err}`);
        injectedClaudeFiles.add(full);
      }
    }
    claudeFirstPoll = false;
  }
  const processedDir = path.join(dir, 'processed');
  for (const name of files) {
    const full = path.join(dir, name);
    if (injectedClaudeFiles.has(full)) {
      continue;
    }
    injectedClaudeFiles.add(full);
    let content = '';
    try {
      content = fs.readFileSync(full, 'utf8');
    } catch (err) {
      log(`claude inbox read failed for ${name}: ${err.message || err}`);
      continue;
    }
    const prompt = extractClaudeInboxPrompt(content);
    if (!prompt) {
      continue;
    }
    const meta = extractClaudeHeader(content, name);
    const sessionId = claudeSessionId(meta);
    const routedPrompt = withClaudeEnvelope(prompt, meta);
    // The broker can't switch the Claude panel's model. When a specific model is
    // required, notify the user to select it; the injected prompt also tells Claude to
    // stop and report a mismatch so a lesser/default model never silently answers.
    const guardMatch = /\[REQUIRED MODEL:\s*([^\]]+)\]/.exec(prompt) || /\[Preferred model:\s*([^\]]+)\]/.exec(prompt);
    if (guardMatch) {
      const reqModel = guardMatch[1].trim();
      const strict = prompt.includes('[REQUIRED MODEL:');
      const msg = strict
        ? `Agent Broker: this Claude request requires model "${reqModel}". Select it in the Claude panel; if it isn't that model Claude will stop and ask you to switch, then re-send.`
        : `Agent Broker: this Claude request prefers model "${reqModel}". Claude will note its model before answering.`;
      Promise.resolve(vscode.window.showWarningMessage(msg)).catch(() => {});
    }
    const result = await injectClaudePrompt(routedPrompt, sessionId);
    if (result.ok) {
      log(`Claude inbox ${name} injected via ${result.method} session=${sessionId || '(new)'} submit=${JSON.stringify(result.submit || {})}`);
      updateStatus('$(radio-tower) Broker: Claude');
    } else {
      log(`Claude inbox ${name} injection failed; opening file for manual paste`);
      try {
        await openTextDocument(full);
      } catch (err) {
        log(`open claude inbox file failed: ${err.message || err}`);
      }
    }
    // Move handled files aside so they are not re-injected on the next reload.
    try {
      fs.mkdirSync(processedDir, { recursive: true });
      fs.renameSync(full, path.join(processedDir, name));
    } catch (err) {
      log(`could not archive claude inbox file ${name}: ${err.message || err}`);
    }
  }
}

async function hasAntigravitySendCommand() {
  // Only Antigravity exposes antigravity.sendPromptToAgentPanel. In plain VS Code the
  // bridge must NOT claim Antigravity requests (claiming without the ability to send
  // strands the row in_progress). Cache a POSITIVE result permanently; re-check a
  // negative one on a short TTL, because the command can register after the first poll
  // (e.g. Antigravity finishing activation) and we must not refuse forever until reload.
  if (antigravitySendSupported === true) {
    return true;
  }
  if (Date.now() - antigravitySendCheckedAt < ANTIGRAVITY_SEND_RECHECK_MS) {
    return false;
  }
  antigravitySendCheckedAt = Date.now();
  try {
    const commands = await vscode.commands.getCommands(true);
    antigravitySendSupported = commands.includes('antigravity.sendPromptToAgentPanel');
  } catch (err) {
    log(`getCommands failed: ${err.message || err}`);
    antigravitySendSupported = false;
  }
  if (!antigravitySendSupported) {
    log('antigravity.sendPromptToAgentPanel not present yet; will re-check (not claiming Antigravity requests here)');
  }
  return antigravitySendSupported;
}

// --- active context snapshots (Phase 2 delivery) ----------------------------------
const snapshotFallbackProcessed = new Set();
const snapshotHeartbeat = { at: 0 };

async function snapshotCapabilities() {
  const caps = [];
  if (await hasAntigravitySendCommand()) caps.push('antigravity');
  if (config().snapshotClaudeCapable) caps.push('claude');
  return caps;
}

async function snapshotHostKind() {
  return (await hasAntigravitySendCommand()) ? 'antigravity' : 'vscode';
}

function currentProjectPath() {
  const folders = vscode.workspace.workspaceFolders || [];
  return folders.length ? folders[0].uri.fsPath : '';
}

async function sendSnapshotHeartbeat() {
  const now = Date.now();
  if (now - snapshotHeartbeat.at < 12000) return;
  snapshotHeartbeat.at = now;
  try {
    const hostKind = await snapshotHostKind();
    const proj = currentProjectPath();
    // Unique host id per window so two windows of the same kind don't overwrite each
    // other's heartbeat row (host is the PK). Carry the real project for routing.
    const hostId = `${hostKind}:${proj || (vscode.env && vscode.env.sessionId) || 'default'}`;
    const caps = (await snapshotCapabilities()).join(',') || '-';
    await runBroker(['heartbeat', hostId, proj || '*', caps, hostKind, String(config().cdpPort || '')]);
  } catch (err) {
    log(`snapshot heartbeat failed: ${err.message || err}`);
  }
}

function snapshotFallbackDirs() {
  const dirs = [path.join(os.homedir(), '.agent-broker', 'context-snapshots')];
  for (const folder of (vscode.workspace.workspaceFolders || [])) {
    dirs.push(path.join(folder.uri.fsPath, '.agent-broker', 'context-snapshots'));
  }
  return dirs;
}

async function scanSnapshotFallbacks() {
  for (const dir of snapshotFallbackDirs()) {
    let names;
    try {
      names = fs.readdirSync(dir).filter(n => n.toLowerCase().endsWith('.md'));
    } catch {
      continue;
    }
    const processedDir = path.join(dir, 'processed');
    for (const name of names) {
      const full = path.join(dir, name);
      if (snapshotFallbackProcessed.has(full)) continue;
      const id = path.basename(name, '.md');
      try {
        const res = await runBroker(['snapshot-complete-file', id, 'fallback_file', full]);
        log(`snapshot fallback ${name} -> ${JSON.stringify(res && res.status)}`);
        // Mark processed only AFTER success, so a transient broker error is retried.
        snapshotFallbackProcessed.add(full);
        fs.mkdirSync(processedDir, { recursive: true });
        fs.renameSync(full, path.join(processedDir, name));
      } catch (err) {
        const msg = String((err && (err.message || err)) || '');
        if (/unknown snapshot request/i.test(msg)) {
          // Not a real request id (stray file): stop retrying it.
          snapshotFallbackProcessed.add(full);
          log(`snapshot fallback ${name} is not a valid request id; skipping`);
        } else {
          log(`snapshot fallback ${name} complete failed (will retry): ${msg}`);
        }
      }
    }
  }
}

async function pollContextSnapshots() {
  if (!config().snapshotPolling) return false;
  await sendSnapshotHeartbeat();
  await scanSnapshotFallbacks();
  const caps = await snapshotCapabilities();
  if (!caps.length) return false;
  let result;
  try {
    result = await runBroker(['snapshot-claim', config().snapshotConsumer, await snapshotHostKind(), caps.join(',')]);
  } catch (err) {
    log(`snapshot-claim failed: ${err.message || err}`);
    return false;
  }
  if (!result || result.status !== 'claimed' || !result.request) return false;
  const req = result.request;
  const prompt = req.snapshot_prompt || '';
  const fam = String(req.family || '').toLowerCase();
  const release = async (why) => {
    log(`snapshot ${req.id} ${why}; releasing back to queued`);
    try { await runBroker(['snapshot-release', req.id]); }
    catch (e) { log(`snapshot release failed: ${e.message || e}`); }
  };
  try {
    if (fam === 'antigravity' && (await hasAntigravitySendCommand())) {
      await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', prompt);
      log(`snapshot ${req.id} delivered to Antigravity panel`);
    } else if (fam === 'claude') {
      const sent = await injectClaudePrompt(prompt, null);
      log(`snapshot ${req.id} delivered to Claude: ${JSON.stringify(sent && sent.ok)}`);
      if (!sent || !sent.ok) {
        try { await openTextDocument(req.fallback_file || ''); } catch {}
      }
    } else {
      await release(`family ${fam} not deliverable from this host`);
    }
  } catch (err) {
    await release(`delivery failed: ${err.message || err}`);
  }
  return true;
}

async function pollOnce() {
  const cfg = config();
  if (!cfg.enabled || busy) {
    return;
  }
  busy = true;
  updateStatus('$(sync~spin) Broker');
  try {
    await completeFallbackFiles();
    await notifyCompletions();
    await pollContextSnapshots();
    await notifyCodexInbox();
    await pollClaudeInbox();
    let claimed = false;
    if (await hasAntigravitySendCommand()) {
      const result = await runBroker(['claim', 'antigravity-extension']);
      if (result.status === 'claimed' && result.request) {
        claimed = true;
        await sendToAntigravity(result.request);
      }
    }
    updateStatus(claimed ? '$(radio-tower) Broker: sent' : '$(check) Broker');
  } catch (err) {
    log(`poll failed: ${err.stack || err.message || err}`);
    updateStatus('$(error) Broker');
  } finally {
    busy = false;
  }
}

async function notifyCompletions() {
  const cfg = config();
  let result;
  try {
    result = await runBroker(['completed-unnotified', '50']);
  } catch (err) {
    log(`completion scan failed: ${err.message || err}`);
    return;
  }
  for (const request of result.items || []) {
    if (notifiedCompletionIds.has(request.id)) {
      continue;
    }
    notifiedCompletionIds.add(request.id);
    await runBroker(['completion-notified', request.id]);
    if (!cfg.showCompletionNotifications) {
      log(`Antigravity completed broker request ${request.id}`);
      continue;
    }
    const action = await vscode.window.showInformationMessage(
      `Antigravity completed broker request: ${request.topic || request.id}`,
      'Open Codex',
      'Open Timeline'
    );
    if (action === 'Open Codex') {
      await openCodexSidebar();
    } else if (action === 'Open Timeline') {
      await openTextDocument(path.join(os.homedir(), '.agent-broker', 'AGENT_BROKER_TIMELINE_HINT.md'), [
        '# Agent Broker Timeline',
        '',
        'Use MCP tool `get_topic_timeline` or broker CLI:',
        '',
        '```powershell',
        `python "${path.join(os.homedir(), '.agent-broker', 'agent_broker_mcp.py')}" bridge requests "*" 20`,
        '```',
        '',
        `Latest completed request: ${request.id}`,
      ].join('\n'));
    }
  }
}

async function notifyCodexInbox() {
  const cfg = config();
  let result;
  try {
    result = await runBroker(['codex-inbox', '*', '50']);
  } catch (err) {
    log(`codex inbox scan failed: ${err.message || err}`);
    return;
  }
  for (const request of result.items || []) {
    if (request.status !== 'queued' || notifiedCodexIds.has(request.id)) {
      continue;
    }
    notifiedCodexIds.add(request.id);
    const file = codexInboxPath(request.id);
    const requestedModel = (request.target_model || '').trim();
    const strictModel = !!request.strict_model;
    const contextPack = await getContextPack(request.project, request.topic, 8000);
    const content = [
      '# Codex Inbox Request',
      '',
      `Request ID: ${request.id}`,
      `From: ${request.created_by || 'antigravity'}`,
      `Project: ${request.project}`,
      `Project path: ${request.root_path || ''}`,
      `Topic: ${request.topic || 'default'}`,
      ...(requestedModel ? [`Requested model: ${requestedModel}${strictModel ? ' (STRICT - do not answer under another model)' : ''}`] : []),
      `Created: ${request.created_at}`,
      '',
      '## Prompt',
      '',
      request.prompt || '',
      '',
      '## Shared Context Pack',
      '',
      contextPack || 'No context pack is available yet.',
      '',
      '## How To Respond',
      '',
      'Open Codex and handle this request in the current topic. Use broker tools to record events or consult other agents.',
    ].join('\n');
    fs.writeFileSync(file, content, 'utf8');
    await runBroker(['codex-notified', request.id]);
    // The broker cannot switch the Codex extension's model itself, so when a specific
    // model is required we surface a notification asking the user to select it. The
    // injected prompt also tells Codex to STOP and report a mismatch if it isn't that
    // model, so a lesser/default model never silently answers.
    if (requestedModel) {
      const msg = strictModel
        ? `Agent Broker: this request requires model "${requestedModel}". Select it in Codex; if Codex isn't that model it will stop and ask you to switch. After switching, re-send the request.`
        : `Agent Broker: this request prefers model "${requestedModel}". Codex will note its model before answering.`;
      Promise.resolve(
        vscode.window.showWarningMessage(msg, 'Open Inbox', 'Open Codex')
      ).then(async (action) => {
        if (action === 'Open Codex') { await openCodexSidebar(); await openTextDocument(file); }
        else if (action === 'Open Inbox') { await openTextDocument(file); }
      }).catch(() => {});
    }
    if (cfg.autoSendCodexInboxToChat) {
      if (cfg.autoOpenCodexSidebar) {
        await openCodexSidebar();
        await delay(500);
      }
      const chatPrompt = [
        '# Agent Broker Callback',
        '',
        `Request ID: ${request.id}`,
        `From: ${request.created_by || 'agent-broker'}`,
        `Project: ${request.project}`,
        `Topic: ${request.topic || 'default'}`,
        '',
        '## Message',
        '',
        request.prompt || '',
        '',
        `Full inbox file: ${file}`,
        '',
        'Continue this same topic. Use broker tools only if more detail is needed.',
      ].join('\n');
      const sent = await runCdpCodexSend(chatPrompt);
      log(`Codex inbox ${request.id} chat delivery result: ${JSON.stringify(sent)}`);
      if (sent && sent.ok) {
        continue;
      }
    }
    if (cfg.autoOpenCodexInbox) {
      if (cfg.autoOpenCodexSidebar) {
        await openCodexSidebar();
      }
      await openTextDocument(file);
      log(`Auto-opened Codex inbox file ${file}`);
      continue;
    }
    if (!cfg.showCodexInboxNotifications) {
      log(`Wrote Codex inbox file ${file}`);
      continue;
    }
    const action = await vscode.window.showInformationMessage(
      `New request for Codex from ${request.created_by || 'Antigravity'}: ${request.topic || request.id}`,
      'Open Codex',
      'Open Inbox'
    );
    if (action === 'Open Codex') {
      await openCodexSidebar();
      await openTextDocument(file);
    } else if (action === 'Open Inbox') {
      await openTextDocument(file);
    }
  }
}

// (Codex callback is single-sourced in the broker's complete_antigravity_request now;
// the former bridge-side extractCodexCallback/queueCodexCallbackFromResponse were removed
// as dead code after that change — see AGENT_BROKER_HANDOFF P0 entry.)

async function openCodexSidebar() {
  const cfg = config();
  const configured = Array.isArray(cfg.codexSidebarCommands) ? cfg.codexSidebarCommands : [];
  let available = [];
  try {
    available = await vscode.commands.getCommands(true);
  } catch (err) {
    log(`getCommands failed: ${err.message || err}`);
  }
  const availableSet = new Set(available);
  for (const command of configured) {
    if (!command) {
      continue;
    }
    if (availableSet.size && !availableSet.has(command)) {
      log(`${command} is not registered in this host`);
      continue;
    }
    if (await tryCommand(command)) {
      log(`Opened Codex sidebar with ${command}`);
      return true;
    }
  }
  const candidates = available
    .filter(command => /(codex|chatgpt|openai)/i.test(command) && /(open|focus|sidebar|view)/i.test(command))
    .slice(0, 20);
  log(`No Codex sidebar command succeeded. Candidate commands: ${candidates.join(', ') || 'none'}`);
  return false;
}

async function openTextDocument(file, content) {
  if (content && !fs.existsSync(file)) {
    fs.writeFileSync(file, content, 'utf8');
  }
  const doc = await vscode.workspace.openTextDocument(file);
  await vscode.window.showTextDocument(doc, { preview: false });
}

function currentProjectPath() {
  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  return folder && folder.uri && folder.uri.fsPath ? folder.uri.fsPath : os.homedir();
}

async function startCompressedChat() {
  const topic = await vscode.window.showInputBox({
    title: 'Agent Broker New Chat',
    prompt: 'Topic name for the compressed shared context',
    value: 'default',
    ignoreFocusOut: true,
  });
  if (topic === undefined) {
    return;
  }
  const project = currentProjectPath();
  const result = await runBroker(['chat-bootstrap', project, topic || 'default', 'antigravity', '5000']);
  const content = result.content || '';
  const file = result.path || path.join(os.homedir(), '.agent-broker', 'new_chat_bootstrap.md');
  try {
    await vscode.commands.executeCommand('antigravity.startNewConversation');
    await delay(600);
    await vscode.commands.executeCommand('antigravity.agentSidePanel.open');
    await delay(300);
    await vscode.commands.executeCommand('antigravity.sendPromptToAgentPanel', content);
    log(`Started compressed Antigravity chat for topic ${topic || 'default'}`);
  } catch (err) {
    log(`start compressed Antigravity chat fallback: ${err.message || err}`);
    await openTextDocument(file, content);
    await vscode.window.showInformationMessage('Opened compressed chat bootstrap. Paste it into the new Codex/Claude/Antigravity chat.');
  }
}

async function completeFallbackFiles() {
  let result;
  try {
    result = await runBroker(['requests', '*', '50']);
  } catch (err) {
    log(`request scan failed: ${err.message || err}`);
    return;
  }
  for (const request of result.items || []) {
    if (request.status !== 'in_progress') {
      continue;
    }
    const file = responsePaths(request).find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).size > 0);
    if (!file) {
      continue;
    }
    log(`Completing request ${request.id} from fallback file`);
    try {
      await runBroker(['complete-file', request.id, request.target_model || 'Antigravity current selected model', file]);
    } catch (err) {
      log(`complete-file failed for ${request.id}: ${err.message || err}`);
      continue;
    }
    // Move the response out of the way so a later requeue of this id can't be
    // re-completed with this stale answer. The broker (complete_antigravity_request)
    // now single-sources the Codex callback, so we no longer extract it here too.
    archiveFallbackFile(file);
  }
}

function archiveFallbackFile(file) {
  try {
    const dir = path.join(path.dirname(file), 'processed');
    fs.mkdirSync(dir, { recursive: true });
    fs.renameSync(file, path.join(dir, path.basename(file)));
  } catch (err) {
    log(`archive fallback ${file} failed: ${err.message || err}`);
    // Best effort: truncate so a stale file can't re-complete a requeued request.
    try { fs.writeFileSync(file, '', 'utf8'); } catch (e2) { /* ignore */ }
  }
}

function schedule() {
  if (timer) {
    clearInterval(timer);
  }
  const cfg = config();
  timer = setInterval(pollOnce, Math.max(1000, cfg.pollIntervalMs));
  pollOnce();
}

function updateStatus(text) {
  if (!statusBar) {
    return;
  }
  statusBar.text = text;
  statusBar.tooltip = 'Agent Broker Bridge';
  statusBar.show();
}

function activate(context) {
  extensionContext = context;
  output = vscode.window.createOutputChannel('Agent Broker Bridge');
  log('activated');
  context.subscriptions.push(output);
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = 'agentBrokerBridge.pollNow';
  context.subscriptions.push(statusBar);
  updateStatus('$(check) Broker');
  context.subscriptions.push(vscode.commands.registerCommand('agentBrokerBridge.pollNow', pollOnce));
  context.subscriptions.push(vscode.commands.registerCommand('agentBrokerBridge.openOutput', () => output.show()));
  context.subscriptions.push(vscode.commands.registerCommand('agentBrokerBridge.startCompressedChat', startCompressedChat));
  context.subscriptions.push(vscode.commands.registerCommand('agentBrokerBridge.resumeModelSelection', async () => {
    await runBroker(['resume-model']);
    await pollOnce();
  }));
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event => {
    if (event.affectsConfiguration('agentBrokerBridge')) {
      log('configuration changed');
      schedule();
    }
  }));
  schedule();
}

function deactivate() {
  if (timer) {
    clearInterval(timer);
  }
}

module.exports = { activate, deactivate };
