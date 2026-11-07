'use strict';

const $ = (id) => document.getElementById(id);
const kinds = ['sessions', 'candidates', 'accounts', 'artifacts', 'monitors', 'deliveries', 'evidence', 'agent_runs', 'reviews', 'signals'];
const accountTabs = ['Overview', 'Locations', 'Evidence', 'Partner data', 'Updates', 'Monitor'];
const inspectorTabs = ['Request', 'Response', 'Grounding', 'Activity'];
const westCoast = 'US West Coast (California, Oregon, Washington)';
const regionStates = { [westCoast]: ['ca', 'california', 'or', 'oregon', 'wa', 'washington'], Texas: ['tx', 'texas'] };
let state = Object.fromEntries(kinds.map((kind) => [kind, []]));
let config = {};
let recording = null;
let recordingId = null;
let selectedId = null;
let selected = new Set();
let activeTab = 'Overview';
let inspectorTab = 'Request';
let activeArtifact = null;
let busy = false;
let polling = false;
let epoch = 0;
let stateController = null;
let detailSignature = '';
let detailCache = null;

function criteriaInput() {
  if (!$('criteria-form').reportValidity()) return null;
  const dates = $('criteria-window-mode').value === 'dates';
  const value = { geography: $('criteria-geography').value.trim(), accounts: $('criteria-accounts').value.trim(), qualifications: $('criteria-qualifications').value.trim(), window_days: dates ? 90 : Number($('criteria-days').value) };
  if (dates) {
    value.window_start = $('criteria-start').value;
    value.window_end = $('criteria-end').value;
    if (value.window_start > value.window_end || value.window_end > new Date().toISOString().slice(0, 10)) throw new Error('Research dates must be ordered and not in the future.');
  }
  return value;
}
function windowMode() {
  const dates = $('criteria-window-mode').value === 'dates';
  $('criteria-dates').hidden = !dates;
  $('days-label').hidden = dates;
  $('criteria-days').required = !dates;
  $('criteria-days').disabled = dates || Boolean(recording);
  for (const id of ['criteria-start', 'criteria-end']) { $(id).required = dates; $(id).disabled = !dates || Boolean(recording); }
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function append(parent, ...children) {
  children.flat().filter(Boolean).forEach((child) => parent.append(child));
  return parent;
}
function text(value, fallback = 'Not reported') {
  if (value === null || value === undefined || value === '') return fallback;
  return typeof value === 'object' ? fallback : String(value);
}
function list(value) { return Array.isArray(value) ? value : []; }
function pretty(value) { return JSON.stringify(value ?? null, null, 2); }
function costDisplay(value) {
  const total = typeof value === 'number' ? value : value?.total;
  return typeof total === 'number' ? `$${total.toFixed(4)}` : value == null ? 'Not reported' : 'See reported breakdown';
}
function date(value, time = false) {
  if (!value) return 'Unknown';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'Unknown';
  return new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC', ...(time ? { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' } : {}) }).format(parsed);
}
function human(value) { return text(value, 'Unknown').replaceAll('_', ' '); }
function badge(value, tone = '') { return node('span', human(value), `badge ${tone}`); }
function fitBadge(fit) { return badge(fit || 'needs_review', fit === 'qualifies' ? 'good' : fit === 'does_not_qualify' ? '' : 'review'); }
function metadata(entries) {
  const dl = node('dl', null, 'metadata');
  entries.forEach(([label, value]) => append(dl, append(node('div'), node('dt', label), append(node('dd'), research(value)))));
  return dl;
}
function rawDetails(label, value) {
  return append(node('details'), node('summary', label), node('pre', pretty(value)));
}
function sourceLink(url, label = 'Open source') {
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    const anchor = node('a', `${label} ↗`, 'source-link');
    anchor.href = parsed.href;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
    return anchor;
  } catch { return null; }
}
function researchLines(value) {
  // Decode double-escaped API layout only when it signals paragraphs or Markdown, not paths like C:\new.
  const parts = String(value).split(/(```[\s\S]*?(?:```|$)|`[^`\n]+`|[A-Za-z]:\\[^\s`]+)/g);
  const escapedLayout = parts.some((part, i) => i % 2 === 0 && /(?<!\\)\\(?:r\\n|n)(?:\\(?:r\\n|n)|\s*(?:#{1,6}\s|[-*+\u2022]\s|\d+[.)]\s))/.test(part));
  return parts.map((part, i) => escapedLayout && i % 2 === 0 ? part.replace(/(?<!\\)\\(?:r\\n|n)/g, '\n') : part).join('').replace(/\r\n?/g, '\n');
}
function inlineResearch(parent, value) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let end = 0;
  for (const match of value.matchAll(pattern)) {
    parent.append(document.createTextNode(value.slice(end, match.index)));
    const token = match[0];
    let child;
    if (token.startsWith('`')) child = node('code', token.slice(1, -1));
    else if (token.startsWith('**') || token.startsWith('__')) child = node('strong', token.slice(2, -2));
    else if (token.startsWith('*')) child = node('em', token.slice(1, -1));
    else {
      const link = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      child = sourceLink(link[2], link[1]);
      if (child) child.textContent = link[1];
    }
    parent.append(child || document.createTextNode(token));
    end = match.index + token.length;
  }
  parent.append(document.createTextNode(value.slice(end)));
  return parent;
}
function research(value, fallback = 'Not reported', className = '') {
  const content = node('div', null, `research-content ${className}`.trim());
  if (value === null || value === undefined || value === '') value = fallback;
  if (Array.isArray(value)) {
    const items = node('ul');
    value.forEach((item) => append(items, append(node('li'), research(item))));
    return append(content, value.length ? items : node('p', fallback));
  }
  if (typeof value === 'object') {
    const fields = node('dl', null, 'research-fields');
    Object.entries(value).forEach(([key, item]) => {
      const link = typeof item === 'string' && /^(?:url|source_url|source|link)$/i.test(key) ? sourceLink(item, item) : null;
      append(fields, append(node('div'), node('dt', human(key)), append(node('dd'), link || research(item))));
    });
    return append(content, fields);
  }
  const lines = researchLines(value).split('\n');
  const marker = (line) => line.match(/^(\s*)([-*+\u2022]|\d+[.)])\s+(.*)$/);
  const lists = [];
  let paragraph = [], code = null, codeLines = 0;
  const flush = () => {
    if (paragraph.length) append(content, inlineResearch(node('p'), paragraph.join('\n')));
    paragraph = [];
  };
  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      flush(); lists.length = 0;
      if (code) code = null;
      else { code = node('code'); codeLines = 0; append(content, append(node('pre'), code)); }
      continue;
    }
    if (code) { code.textContent += `${codeLines++ ? '\n' : ''}${line}`; continue; }
    if (!line.trim()) { flush(); continue; }
    const heading = line.match(/^\s*(#{1,6})\s+(.+?)(?:\s+#+)?$/);
    const item = marker(line);
    if (heading) {
      flush(); lists.length = 0;
      append(content, inlineResearch(node(`h${Math.min(6, heading[1].length + 3)}`), heading[2]));
    } else if (item) {
      flush();
      const indent = item[1].replaceAll('\t', '    ').length;
      const tag = /^\d/.test(item[2]) ? 'ol' : 'ul';
      while (lists.length && (lists.at(-1).indent > indent || (lists.at(-1).indent === indent && lists.at(-1).tag !== tag))) lists.pop();
      if (!lists.length || lists.at(-1).indent < indent) {
        const element = node(tag);
        if (tag === 'ol') element.start = parseInt(item[2], 10);
        append(lists.at(-1)?.last || content, element);
        lists.push({ indent, tag, element });
      }
      const current = lists.at(-1);
      current.last = inlineResearch(node('li'), item[3]);
      append(current.element, current.last);
    } else if (lists.length && /^\s+/.test(line)) {
      const target = lists.at(-1).last;
      target.append(document.createTextNode('\n')); inlineResearch(target, line.trim());
    } else {
      lists.length = 0;
      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) { flush(); append(content, inlineResearch(node('blockquote'), quote[1])); }
      else paragraph.push(line);
    }
  }
  flush();
  return content;
}
function button(label, callback, { write = false, disabled = false, primary = false } = {}) {
  const element = node('button', label, primary ? 'primary' : '');
  element.type = 'button';
  if (write) element.dataset.write = 'true';
  element.disabled = disabled || (write && Boolean(recording));
  element.addEventListener('click', () => perform(callback, element));
  return element;
}
function error(error) {
  if (error.name === 'AbortError') return;
  $('error-message').textContent = error.message || String(error);
  $('error-banner').hidden = false;
  $('error-banner').scrollIntoView({ block: 'nearest' });
  const dialog = document.querySelector('dialog[open]');
  if (dialog && dialog.id !== 'confirm-dialog') {
    dialog.querySelector('.dialog-error')?.remove();
    const message = node('p', error.message || String(error), 'inline-note error-note dialog-error');
    message.setAttribute('role', 'alert');
    dialog.prepend(message);
  }
}
function notice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}
async function api(path, { method = 'GET', body, signal } = {}) {
  if (recording) throw new Error('Recorded session is read-only. Exit recording to use live controls.');
  const response = await fetch(path, { method, headers: { Accept: 'application/json', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) }, body: body === undefined ? undefined : JSON.stringify(body), signal });
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = value?.detail || value?.error;
    throw new Error(Array.isArray(detail) ? detail.map((item) => `${list(item.loc).join('.')}: ${item.msg}`).join('; ') : text(detail, `Request failed (${response.status}).`));
  }
  if (!value) throw new Error('The server returned an empty or invalid response.');
  return value;
}
async function perform(callback, element) {
  if (busy) { notice('An action is already in progress. Finish or cancel the open confirmation first.'); return; }
  busy = true;
  $('busy-status').textContent = `${element?.textContent?.trim() || 'Workspace action'} in progress. Waiting for confirmation or server response...`;
  $('busy-status').hidden = false;
  $('error-banner').hidden = true;
  const wasDisabled = element?.disabled;
  if (element) element.disabled = true;
  updateControls();
  document.body.setAttribute('aria-busy', 'true');
  try { await callback(); } catch (issue) { error(issue); }
  finally {
    busy = false;
    $('busy-status').hidden = true;
    document.body.removeAttribute('aria-busy');
    if (element?.isConnected) element.disabled = wasDisabled || (element.dataset.write === 'true' && Boolean(recording));
    updateControls();
  }
}
function confirmPaid(description) {
  if (recording) return Promise.resolve(false);
  $('confirm-description').textContent = description;
  const dialog = $('confirm-dialog');
  return new Promise((resolve) => {
    let accepted = false;
    $('confirm-proceed').onclick = () => { accepted = true; dialog.close(); };
    $('confirm-cancel').onclick = () => dialog.close();
    dialog.addEventListener('close', () => resolve(accepted), { once: true });
    dialog.showModal();
    $('confirm-cancel').focus();
  });
}
async function mutation(path, body = {}, paidDescription = null, method = 'POST') {
  if (recording) throw new Error('Recorded session is read-only.');
  if (paidDescription && !await confirmPaid(paidDescription)) return null;
  const result = await api(path, { method, body });
  await refresh(true);
  return result;
}
function normalizeState(value) {
  const normalized = Object.fromEntries(kinds.map((kind) => [kind, list(value?.[kind])]));
  normalized.accounts = normalized.accounts.filter((account) => !account.merged_into);
  return normalized;
}
function detailFromState(id) {
  const account = state.accounts.find((item) => item.id === id);
  if (!account) return null;
  const detail = { account };
  for (const kind of ['evidence', 'agent_runs', 'reviews', 'signals', 'artifacts', 'monitors', 'deliveries']) detail[kind] = state[kind].filter((item) => item.account_id === id);
  return detail;
}
async function refresh(force = false) {
  if (recording || (polling && !force)) return;
  if (force) stateController?.abort();
  polling = true;
  const controller = new AbortController();
  stateController = controller;
  const version = epoch;
  try {
    const value = await api('/api/state', { signal: controller.signal });
    if (version !== epoch || recording) return;
    state = normalizeState(value);
    $('connection').textContent = 'Local workspace';
    $('connection').classList.remove('offline');
    render(force);
  } catch (issue) {
    if (issue.name !== 'AbortError' && version === epoch && !recording) {
      $('connection').textContent = 'Connection interrupted';
      $('connection').classList.add('offline');
      error(issue);
    }
  } finally { if (stateController === controller) polling = false; }
}
function currentSession() {
  const account = state.accounts.find((item) => item.id === selectedId);
  return state.sessions.find((item) => item.id === account?.session_id) || [...state.sessions].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
}
function render(force = false) {
  const session = currentSession();
  const account = state.accounts.find((item) => item.id === selectedId);
  $('research-geography').textContent = account ? (account.geography || 'Texas') : session ? (session.geography || 'Texas') : recording ? 'Texas' : westCoast;
  const end = session?.window_end || new Date().toISOString();
  const start = session?.window_start || new Date(new Date(end).getTime() - 90 * 86400000).toISOString();
  $('window-dates').textContent = `${date(start)} – ${date(end)}`;
  $('window-caption').textContent = session ? `Created ${date(session.created_at)} · UTC` : '90-day preview';
  $('account-count').textContent = state.accounts.filter((a) => a.last_researched).length;
  $('qualified-count').textContent = state.accounts.filter((a) => a.fit === 'qualifies').length;
  $('review-count').textContent = state.accounts.filter((a) => !a.fit || a.fit === 'needs_review' || a.needs_recheck).length;
  $('ledger-count').textContent = state.accounts.length;
  $('candidate-count').textContent = state.candidates.length;
  const reported = state.artifacts.filter((a) => a.cost_dollars != null).sort((a, b) => String(b.ended_at || b.started_at).localeCompare(String(a.ended_at || a.started_at)));
  $('reported-cost').textContent = costDisplay(reported[0]?.cost_dollars);
  $('reported-cost').title = reported[0] ? `${human(reported[0].operation)}: ${text(reported[0].cost_dollars)}` : 'No cost returned';
  for (const id of selected) if (!state.accounts.some((a) => a.id === id)) selected.delete(id);
  renderAccounts();
  renderProgress();
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if (force || !editing) renderCandidates();
  const detail = detailFromState(selectedId);
  const signature = JSON.stringify(detail);
  if (force || (signature !== detailSignature && !editing)) {
    detailCache = detail;
    detailSignature = signature;
    renderDetail();
  }
  updateControls();
  const failed = state.sessions.filter((s) => s.error || s.status === 'failed');
  if (failed.length && $('error-banner').hidden) error(new Error(`Discovery ${human(failed.at(-1).status)}: ${text(failed.at(-1).error, 'No successful discovery result was returned.')}`));
}
function updateControls() {
  const readOnly = Boolean(recording);
  document.querySelectorAll('[data-stage]').forEach((element) => { element.disabled = busy; });
  for (const id of ['discover', 'save-recording', 'settings-button']) $(id).disabled = readOnly || busy;
  $('research').disabled = readOnly || busy || selected.size === 0;
  $('open-recording').disabled = busy;
  $('selection-label').textContent = readOnly ? 'Read-only' : `${selected.size} selected`;
  document.querySelectorAll('[data-write]').forEach((element) => { if (readOnly) element.disabled = true; });
  $('criteria-form').querySelectorAll('input, textarea, select').forEach((element) => { element.disabled = readOnly; });
  windowMode();
}
function renderProgress() {
  const panel = $('research-progress');
  panel.replaceChildren();
  panel.hidden = !state.agent_runs.length;
  if (panel.hidden) return;
  append(panel, node('h2', 'Research progress'));
  state.agent_runs.forEach((run) => {
    const account = state.accounts.find((a) => a.id === run.account_id);
    if (!account) return;
    const row = append(node('div', null, 'progress-row'), button(account.name, () => selectAccount(account.id)), badge(run.status), node('span', human(run.stage || 'waiting for worker'), 'muted'));
    if (run.queue_note || run.error) append(row, node('p', text(run.error || run.queue_note), 'inline-note error-note'));
    if (['interrupted', 'ambiguous'].includes(run.status) && !run.remote_id) append(row, node('p', 'No remote run ID was saved. Inspect the original request before starting anything else; a paid create is never retried automatically.', 'inline-note'));
    if (run.remote_id && !['completed', 'failed', 'cancelled', 'invalid_output'].includes(run.status)) append(row, button('Refresh saved run', () => mutation(`/api/agent-runs/${encodeURIComponent(run.id)}/poll`), { write: true }));
    panel.append(row);
  });
}
function renderAccounts() {
  const query = $('account-search').value.toLowerCase();
  const fit = $('fit-filter').value;
  const accounts = state.accounts.filter((a) => `${a.name} ${a.domain}`.toLowerCase().includes(query) && (fit === 'all' || (a.fit || 'needs_review') === fit));
  const tbody = $('account-rows');
  tbody.replaceChildren();
  accounts.forEach((account) => {
    const tr = node('tr', null, selectedId === account.id ? 'selected' : '');
    const checkbox = node('input');
    checkbox.type = 'checkbox';
    checkbox.checked = selected.has(account.id);
    checkbox.disabled = Boolean(recording) || !(account.domain_verified || account.domain_accepted);
    checkbox.setAttribute('aria-label', `Select ${account.name} for research`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && selected.size >= 5) { checkbox.checked = false; error(new Error('Select at most five accounts for research.')); return; }
      if (checkbox.checked) selected.add(account.id); else selected.delete(account.id);
      updateControls();
    });
    const name = button(account.name || 'Unnamed account', () => selectAccount(account.id));
    name.className = 'company-button';
    name.setAttribute('aria-pressed', String(selectedId === account.id));
    const company = append(node('td'), name, node('span', text(account.domain, 'Domain not confirmed'), 'domain'), node('span', account.geography || 'Texas', 'domain'));
    const fitCell = append(node('td'), fitBadge(account.fit));
    if (account.needs_recheck) fitCell.append(badge('Needs recheck', 'review'));
    const run = state.agent_runs.find((r) => r.account_id === account.id);
    if (run) fitCell.append(badge(`Research ${human(run.status)}`));
    const locations = list(account.locations);
    const found = account.locations_found ?? locations.filter((l) => l.operating_status === 'operating' && l.supported === true && !l.duplicate && (regionStates[account.geography || 'Texas'] || []).includes((l.state || '').trim().toLowerCase())).length;
    append(tr, append(node('td'), checkbox), company, fitCell, node('td', account.assessment_basis === 'source-grounded Agent assessment' ? 'See criteria assessment' : `${found} supported operating`), node('td', account.last_researched ? date(account.last_researched) : 'Not researched'));
    tbody.append(tr);
  });
  $('ledger-empty').hidden = state.accounts.length > 0;
  if (state.accounts.length && !accounts.length) {
    const td = node('td', 'No accounts match these filters.', 'muted');
    td.colSpan = 5;
    tbody.append(append(node('tr'), td));
  }
}
async function selectAccount(id) {
  selectedId = id;
  activeTab = 'Overview';
  detailCache = detailFromState(id);
  render(true);
  if (!recording) {
    const version = epoch;
    const detail = await api(`/api/accounts/${encodeURIComponent(id)}`);
    if (version === epoch && selectedId === id) { detailCache = detail; renderDetail(); }
  }
  if (matchMedia('(max-width: 1100px)').matches) $('detail-content').scrollIntoView({ behavior: 'instant', block: 'start' });
}
function tabs(containerId, names, active, onChange, panelId) {
  const container = $(containerId);
  container.replaceChildren();
  names.forEach((name, index) => {
    const tab = node('button', name);
    tab.id = `${containerId}-${index}`;
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(name === active));
    tab.setAttribute('aria-controls', panelId);
    tab.tabIndex = name === active ? 0 : -1;
    tab.addEventListener('click', () => { onChange(name); $(`${containerId}-${index}`).focus(); });
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? names.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + names.length) % names.length;
      onChange(names[next]);
      $(`${containerId}-${next}`).focus();
    });
    container.append(tab);
    if (name === active) $(panelId).setAttribute('aria-labelledby', tab.id);
  });
}
function renderDetail() {
  const detail = detailCache;
  $('account-detail').hidden = !detail;
  $('detail-content').hidden = !detail;
  if (!detail) return;
  const account = detail.account;
  const heading = $('detail-heading');
  heading.replaceChildren();
  append(heading, append(node('div', null, 'detail-meta'), append(node('div'), node('h2', account.name || 'Unnamed account'), node('span', text(account.domain, 'Unconfirmed domain'), 'domain')), button('Inspect request', () => openInspector(account.id))));
  append(heading, append(node('div', null, 'badges'), fitBadge(account.fit), badge(account.entity_verification_status === 'verified' ? 'Entity source-supported' : account.domain_accepted ? 'Source domain accepted / entity unverified' : account.domain_verified ? 'Domain manually confirmed' : 'Domain unverified', account.entity_verification_status === 'verified' ? 'good' : 'review'), account.needs_recheck ? badge('Needs recheck', 'review') : null));
  tabs('account-tabs', accountTabs, activeTab, (tab) => { activeTab = tab; renderDetail(); }, 'account-panel');
  const panel = $('account-panel');
  panel.replaceChildren();
  ({ Overview: renderOverview, Locations: renderLocations, Evidence: renderEvidence, 'Partner data': renderPartner, Updates: renderUpdates, Monitor: renderMonitor })[activeTab](panel, detail);
}
function renderOverview(panel, detail) {
  const a = detail.account;
  append(panel, node('h3', 'Assessment'));
  const explanation = typeof a.fit_explanation === 'string' ? a.fit_explanation.replace(/ +Application checks:/g, '\n\nApplication checks:') : a.fit_explanation;
  append(panel, research(explanation, 'Not researched', 'fit-explanation'));
  Object.entries(a.criteria_assessment || {}).forEach(([key, value]) => append(panel, append(node('section', null, 'assessment-block'), node('h4', human(key)), badge(value.supported ? value.status : 'Needs review'), research(value.explanation), ...Object.values(value.provenance || {}).flat().map((citation) => sourceLink(citation.url, citation.title || 'Criterion source')), rawDetails('Criterion grounding metadata', value.provenance))));
  append(panel, button('Monitor checks', () => { activeTab = 'Monitor'; renderDetail(); }));
  renderFeatures(panel, detail);
  append(panel, append(node('details'), node('summary', 'Saved research criteria and metadata'), metadata([['Geography', a.geography || 'Texas'], ['Accounts', a.accounts || 'Regional fitness chains'], ['Qualifications', a.qualifications || '2+ operating locations and expansion'], ['Assessment basis', a.assessment_basis || 'Legacy/default checks'], ['Research cutoff', date(a.window_end)], ['Window starts', date(a.window_start)], ['Monitor lookback days', a.window_days || 90], ['Last researched', a.last_researched ? date(a.last_researched, true) : 'Not researched'], ['Employee count / range', a.employee_count ?? 'Unknown']])));
  if (a.unresolved_questions?.length) append(panel, append(node('section'), node('h3', `Unresolved questions (${a.unresolved_questions.length})`), research(a.unresolved_questions)));
  append(panel, rawDetails('Field provenance and application rules', a.provenance || { status: 'Unsupported / needs review' }));
  const runs = list(detail.agent_runs);
  if (runs.length) append(panel, node('hr', null, 'section-rule'), node('h3', 'Research activity'));
  runs.forEach((run) => {
    const record = append(node('section', null, 'record'), node('h4', `Agent research / ${human(run.status)}`));
    append(record, node('p', `Run ${run.remote_id || run.id}`, 'status-line'));
    if (run.error) append(record, node('p', text(run.error), 'inline-note error-note'));
    if (!recording && run.remote_id && !['completed', 'cancelled', 'failed', 'invalid_output'].includes(run.status)) append(record, button('Refresh run status', () => mutation(`/api/agent-runs/${encodeURIComponent(run.id)}/poll`), { write: true }));
    panel.append(record);
  });
  if (!recording && (a.domain_verified || a.domain_accepted) && runs.length && runs.every((run) => ['failed', 'cancelled'].includes(run.status) && !run.web_only)) append(panel, button('Research with web only', async () => {
    const result = await mutation('/api/research', { account_ids: [a.id], web_only: true }, `Start a separate web-only research run for ${a.name}. Fiber will not be requested. This is not a retry of an existing run.`);
    if (result) notice('Web-only research started.');
  }, { write: true }));
  if (list(detail.reviews).length) {
    append(panel, node('hr', null, 'section-rule'), node('h3', 'Human review history'));
    detail.reviews.forEach((review) => append(panel, append(node('details', null, 'record'), node('summary', `${human(review.decision)} / ${date(review.created_at)}`), research(review.note, 'No note'), rawDetails('Review record', review))));
  }
  if (!recording && state.accounts.length > 1) {
    const disclosure = append(node('details', null, 'record'), node('summary', 'Merge duplicate'));
    const select = node('select');
    select.setAttribute('aria-label', 'Keep this target account');
    state.accounts.filter((other) => other.id !== a.id && (other.geography || 'Texas') === (a.geography || 'Texas')).forEach((other) => {
      const option = node('option', `${other.name} / ${other.domain}`); option.value = other.id; select.append(option);
    });
    append(disclosure, select, button('Merge duplicate', async () => {
      if (!window.confirm(`Merge ${a.name} into ${select.selectedOptions[0].textContent}? Evidence and history will be preserved.`)) return;
      const targetId = select.value;
      await mutation(`/api/accounts/${encodeURIComponent(a.id)}/merge`, { target_id: targetId });
      await selectAccount(targetId);
    }, { write: true }));
    panel.append(disclosure);
  }
}
function renderFeatures(panel, detail) {
  const evidence = list(detail.evidence);
  const runs = list(detail.agent_runs);
  const search = evidence.filter((e) => e.source === 'search');
  const agent = evidence.filter((e) => e.source === 'agent_grounding');
  const monitor = list(detail.monitors)[0];
  const features = append(node('section', null, 'feature-usage'), node('h3', 'Exa feature activity'));
  const entries = [
    ['Search', `${search.length} saved sources. Location lookup: ${human(detail.account.location_search_status || 'not_started')}.`, search],
    ['Agent', runs.length ? runs.map((r) => `${human(r.status)} (${r.remote_id || r.id})`).join('; ') : 'Not started.', agent],
    ['Places', `Places uses the Search endpoint. ${evidence.filter((e) => e.purpose === 'locations').length} location-search sources saved; Places-specific attribution is unconfirmed. Inspect raw returned metadata, not inferred addresses.`, evidence.filter((e) => e.purpose === 'locations')],
    ['Connect / Fiber', detail.account.partner_requested || runs.some((r) => r.partner_requested) ? 'Fiber requested through Agent dataSources. Provider use and field attribution are unconfirmed; a request or headcount alone is not proof.' : 'Not requested.', []],
    ['Monitor', monitor ? `${human(monitor.status)}. ${list(detail.deliveries).length} saved deliveries; ${evidence.filter((e) => e.source === 'monitor').length} source references.` : 'Not created. Open Monitor checks to create, check now, schedule or pause.', evidence.filter((e) => e.source === 'monitor')],
    ['Contents', `${evidence.filter((e) => e.source === 'contents').length} saved context retrieval attempts. See Evidence for returned passages and failures.`, evidence.filter((e) => e.source === 'contents')],
  ];
  entries.forEach(([name, status, sources]) => {
    const row = append(node('div', null, 'feature-row'), node('h4', name), node('p', status));
    if (sources.length) append(row, append(node('details'), node('summary', `Sources (${sources.length})`), ...sources.map((e) => sourceLink(e.url, text(e.title || e.field, 'Source')))));
    features.append(row);
  });
  if (runs.length) append(features, rawDetails('Agent lifecycle evidence (no provider attribution inferred)', runs.map((r) => ({ id: r.id, events: r.events, events_error: r.events_error }))));
  append(features, button('Inspect feature requests', () => openInspector(detail.account.id)));
  panel.append(features);
}
function renderLocations(panel, detail) {
  append(panel, node('h3', 'Locations found'));
  const locations = list(detail.account.locations);
  append(panel, node('p', 'Exa Places is included automatically for physical-place queries through Search (auto). A returned web page is not proof of Places coverage. Inline page text and original sources are retained; Agent checks operating versus announced branches.', 'inline-note'));
  if (!locations.length) append(panel, node('p', 'No location records have been returned.', 'inline-note'));
  locations.forEach((location) => {
    const record = append(node('section', null, 'record location-record'), node('h4', text(location.name, 'Unnamed location')));
    append(record, metadata([['Label', location.name || 'Unnamed location'], ['Address', location.address || 'Address unknown'], ['Operating status', human(location.operating_status || 'unclear')], ['State', location.state || 'Unknown'], ['Operating entity', location.operating_entity || 'Unconfirmed'], ['Evidence status', location.supported ? 'Supported' : 'Unsupported / needs review']]));
    if (location.duplicate) append(record, node('p', 'Duplicate site: excluded from the operating-location count.', 'inline-note'));
    const sources = append(node('div', null, 'source-meta'), node('h5', 'Location sources'));
    const citations = Object.values(location.provenance || {}).flat();
    const seen = new Set();
    citations.forEach((citation) => {
      const link = sourceLink(citation.url, citation.title || citation.url);
      if (link && !seen.has(link.href)) { append(sources, link); seen.add(link.href); }
    });
    if (!seen.size) append(sources, node('p', 'No source link returned.', 'muted'));
    append(record, sources);
    append(record, rawDetails('Location evidence and metadata', location));
    panel.append(record);
  });
  append(panel, node('h4', 'Location query'), research(detail.location_request?.query || `Physical locations of ${detail.account.name} (${detail.account.domain}) in ${detail.account.geography || 'Texas'}: addresses, opening hours and operating company.`, '', 'query-preview'));
  const evidence = list(detail.evidence).filter((e) => e.purpose === 'locations');
  if (evidence.length) append(panel, node('h3', 'Location source excerpts'));
  evidence.forEach((e) => append(panel, evidenceCard(e, detail.account.id)));
}
function evidenceCard(evidence, accountId) {
  const record = append(node('section', null, 'record evidence-record'), node('h4', text(evidence.title, 'Source evidence')));
  append(record, append(node('div', null, 'badges'), badge(text(evidence.source, 'Source unconfirmed')), evidence.synthetic ? badge('Synthetic test', 'review') : null));
  const passages = node('div', null, 'evidence-passages');
  if (evidence.source === 'contents') {
    append(record, badge(evidence.status || 'needs_review', evidence.error ? 'error' : 'review'));
    append(passages, node('h5', 'Previous passage'));
    append(passages, append(node('blockquote'), research(evidence.previous_passage, 'No previous passage available in this record.')));
    append(passages, node('h5', 'Fuller context'));
  }
  const highlights = Array.isArray(evidence.highlights) ? evidence.highlights : evidence.highlights ? [evidence.highlights] : [];
  highlights.forEach((highlight) => append(passages, append(node('blockquote'), research(highlight))));
  if (evidence.excerpt) append(passages, append(node('blockquote'), research(evidence.excerpt)));
  if (evidence.text) append(passages, append(node('details', null, 'page-text'), node('summary', 'Read returned page text'), research(evidence.text)));
  if (!highlights.length && !evidence.excerpt && !evidence.text) append(passages, node('p', 'No excerpt returned. Review the source and grounding metadata.', 'muted'));
  append(record, passages);
  if (evidence.error) append(record, research(evidence.error, '', 'inline-note error-note'));
  append(record, append(node('div', null, 'source-meta'), node('h5', 'Source details'), sourceLink(evidence.url), metadata([['Published', date(evidence.publication_date)], ['Discovered', date(evidence.discovered_at, true)]])));
  append(record, rawDetails('Source record and metadata basis', evidence));
  if (!recording) {
    const form = append(node('section', null, 'review-form'), node('h4', 'Review and Contents context'));
    const query = node('input'); query.placeholder = 'What needs clarification?'; query.setAttribute('aria-label', 'Context question');
    const note = node('textarea'); note.placeholder = 'Review note: what does this evidence establish?'; note.setAttribute('aria-label', 'Review note');
    append(form, query, button('Read more context', async () => {
      const result = await mutation(`/api/accounts/${encodeURIComponent(accountId)}/context`, { evidence_id: evidence.id, ...(query.value.trim() ? { query: query.value.trim() } : {}) }, 'Fetch fuller context for this known source through Exa Contents. Maximum two extra pages per account; the previous passage is preserved.');
      if (result) notice('Context retrieved.');
    }, { write: true, disabled: !evidence.url || (state.accounts.find((a) => a.id === accountId)?.context_reads || 0) >= 2 }), note);
    append(form, append(node('div', null, 'button-row'), ...[['Confirm finding', 'confirm'], ['Mark unresolved', 'unresolved']].map(([label, decision]) => button(label, async () => {
      await mutation(`/api/accounts/${encodeURIComponent(accountId)}/reviews`, { decision, note: note.value, evidence_id: evidence.id });
      notice('Review decision saved with the evidence reference.');
    }, { write: true }))));
    record.append(form);
  }
  return record;
}
function renderEvidence(panel, detail) {
  append(panel, node('h3', 'Evidence'));
  const evidence = list(detail.evidence);
  if (!evidence.length) append(panel, node('p', 'No evidence saved for this account.', 'inline-note'));
  evidence.forEach((item) => panel.append(evidenceCard(item, detail.account.id)));
}
function renderPartner(panel, detail) {
  const account = detail.account;
  append(panel, node('h3', 'Partner data'), metadata([['Partner requested', account.partner_requested ? 'Fiber' : 'Not requested'], ['Employee count / range', account.employee_count ?? 'Unknown']]));
  append(panel, node('p', 'Exa Connect / Fiber is requested in Agent dataSources, not a separate fabricated enrichment call. Requested does not mean used. Field attribution remains unconfirmed unless the returned evidence explicitly establishes the provider.', 'inline-note'));
  Object.entries(account.provenance || {}).forEach(([field, value]) => append(panel, append(node('section', null, 'assessment-block'), node('h4', human(field)), node('p', `Evidence: ${value.status || 'unconfirmed'} / Provider: ${value.provider || 'unconfirmed'}`), ...list(value.citations).map((citation) => sourceLink(citation.url, citation.title || 'Field source')))));
  append(panel, rawDetails('Field provenance', account.provenance || { status: 'Field provenance unconfirmed' }));
  list(detail.agent_runs).filter((run) => run.error).forEach((run) => append(panel, node('p', text(run.error), 'inline-note error-note')));
}
function renderUpdates(panel, detail) {
  append(panel, node('h3', 'Expansion timeline'));
  const signals = [...list(detail.account.expansion_signals), ...list(detail.signals)];
  if (!signals.length) append(panel, node('p', 'No recorded signals', 'muted'));
  signals.forEach((signal) => {
    const record = node('section', null, 'record');
    append(record, append(node('div', null, 'record-title'), node('h4', human(signal.type || 'Expansion signal')), badge(signal.status || (signal.supported ? 'supported' : 'needs_review'), signal.supported || signal.status === 'supported' ? 'good' : 'review')), research(signal.description, 'Description not supplied'), metadata([['Event date', date(signal.event_date)], ['Published', date(signal.publication_date)], ['Discovered', date(signal.discovered_at, true)], ['Date status', human(signal.event_date_status || signal.date_status)]]), sourceLink(signal.source_url));
    list(signal.citations).forEach((citation) => append(record, sourceLink(citation.url, text(citation.title, 'Open source'))));
    Object.values(signal.provenance || {}).flat().forEach((citation) => append(record, sourceLink(citation.url, citation.title || 'Signal source')));
    append(record, rawDetails('Signal grounding and date checks', signal));
    panel.append(record);
  });
  if (detail.account.needs_recheck) append(panel, node('p', 'New evidence or unresolved review requires re-review. Open Evidence to save a decision against its source; automatic updates do not overwrite human decisions.', 'inline-note'));
  append(panel, button('Review update sources', () => { activeTab = 'Evidence'; renderDetail(); }));
  const deliveries = list(detail.deliveries);
  append(panel, node('hr', null, 'section-rule'), node('h3', 'Incoming deliveries'));
  if (!deliveries.length) append(panel, node('p', 'No verified monitor deliveries received.', 'muted'));
  deliveries.forEach((delivery) => {
    const record = append(node('details', null, 'record'), node('summary', `${date(delivery.received_at, true)} / ${human(delivery.status)}`));
    append(record, append(node('div', null, 'record-title'), node('h4', delivery.synthetic || recording?.synthetic ? 'Synthetic test' : delivery.verified ? recording ? 'Saved delivery: verification claimed' : 'Verified delivery' : 'Unverified delivery'), badge(delivery.status, delivery.error ? 'error' : '')), node('p', `${delivery.id} · ${date(delivery.received_at, true)}`, 'status-line'));
    if (delivery.error || ['failed', 'error'].includes(delivery.status)) append(record, node('p', text(delivery.error, 'Delivery failed. No successful check has been assumed.'), 'inline-note error-note'));
    else if (delivery.status === 'applied' && delivery.added === 0) append(record, node('p', 'No new findings', 'inline-note'));
    else if (delivery.added !== undefined) append(record, node('p', `${delivery.added} findings added`));
    append(record, rawDetails('Saved delivery payload', delivery.payload));
    if (recording && !recordingId && delivery.verified) append(record, node('p', 'Exit recording and reopen this file to register its isolated replay snapshot.', 'inline-note'));
    if (delivery.verified && delivery.pre_update && (!recording || recordingId)) append(record, button('Replay delivery', async () => {
      let result;
      if (recording) {
        // This is the sole recorded-mode request: a disposable sandbox, never live state or Exa.
        const response = await fetch(`/api/replay/${encodeURIComponent(delivery.id)}?recording_id=${encodeURIComponent(recordingId)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        result = await response.json();
        if (!response.ok) throw new Error(text(result.detail, 'Isolated replay failed.'));
        if (result.isolated !== true) throw new Error('Replay did not confirm sandbox isolation. No successful result has been assumed.');
      } else result = await api(`/api/replay/${encodeURIComponent(delivery.id)}`, { method: 'POST', body: {} });
      const output = append(node('div', null, 'replay-result'), node('h4', delivery.synthetic || recording?.synthetic ? 'Synthetic test' : 'Replay of recorded delivery'), node('p', 'Sandbox only. Applied once, then applied again to verify duplicate handling. The snapshot and live records are unchanged.'), node('p', text(result.verification_basis, 'Verification basis not reported'), 'inline-note'), metadata([['Before / accounts', result.before?.accounts], ['Before / signals', result.before?.signals], ['After first / accounts', result.after_first?.accounts], ['After first / signals', result.after_first?.signals], ['After duplicate / accounts', result.after_second?.accounts], ['After duplicate / signals', result.after_second?.signals], ['Second application', result.second?.status]]), rawDetails('Replay result', result));
      record.querySelector('.replay-result')?.remove(); record.append(output);
    }));
    panel.append(record);
  });
}
function renderMonitor(panel, detail) {
  const account = detail.account;
  const monitor = list(detail.monitors)[0];
  append(panel, node('h3', 'Monitor'));
  const preview = append(node('section', null, 'monitor-preview'), node('h4', 'Query, extracted fields and webhook preview'), node('p', 'Loading local preview. No monitor is created.'));
  panel.append(preview);
  if (!recording) api(`/api/accounts/${encodeURIComponent(account.id)}/monitor/preview`).then((value) => {
    if (!preview.isConnected) return;
    const request = value.request;
    preview.replaceChildren();
    append(preview, node('h4', 'Query, extracted fields and webhook preview'), node('p', request.search.query, 'query-preview'), metadata([['Attach to existing account', `${account.name} / ${request.metadata.account_id}`], ['Extracted fields', Object.keys(request.outputSchema.properties.signals.items.properties).join(', ')], ['Webhook', request.webhook.url], ['Events', request.webhook.events.join(', ')]]), node('p', value.note, 'muted'));
    if (!value.receiver_configured) append(preview, node('p', 'Receiver not configured. Preview uses an example URL; configure a real public receiver before creation.', 'inline-note'));
  }).catch((issue) => { if (preview.isConnected) preview.replaceChildren(node('p', `Preview unavailable: ${issue.message}`, 'inline-note error-note')); });
  else preview.replaceChildren(node('p', 'Recording is read-only. Saved monitor configuration appears below.', 'muted'));
  append(panel, node('p', `Checks use this account's saved criteria and a rolling ${account.window_days || 90}-day window. Creating a monitor does not run it or enable a schedule.`, 'inline-note'));
  if (!monitor) {
    append(panel, node('p', 'No monitor', 'muted'));
    if (!recording) append(panel, button('Create monitor', () => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor`, {}, `Create a manual-only monitor for ${account.name}. Requires an accepted source domain and a configured public HTTPS receiver. Scheduling is not enabled by this action.`), { write: true, primary: true, disabled: !(account.domain_verified || account.domain_accepted) || !config.receiver_url }));
    if (!recording) append(panel, node('p', config.receiver_url ? `Receiver configured: ${config.receiver_url}. Keep the separate receiver running and publicly reachable for signed deliveries.` : 'A public HTTPS URL for the separate receiver (port 8788) is required. The private UI is not a webhook receiver.', 'status-line'), button('Configure receiver', openSettings));
    append(panel, node('p', 'After creation: Check now runs one paid check. Enable daily checks starts recurring paid runs. Pause stops the schedule.', 'inline-note'));
    return;
  }
  append(panel, metadata([['Monitor ID', monitor.remote_id], ['Status', human(monitor.status)], ['Schedule', monitor.trigger || 'Manual only'], ['Next run', monitor.next_run_at ? date(monitor.next_run_at, true) : 'Not scheduled'], ['Last checked', date(monitor.last_checked, true)], ['Last received update', date(monitor.last_received_update, true)]]));
  if (monitor.last_error || ['failed', 'creation_uncertain'].includes(monitor.status)) append(panel, node('p', text(monitor.last_error, 'Monitor creation outcome is uncertain. Reconcile the original request before trying again.'), 'inline-note error-note'));
  else if (monitor.last_result) append(panel, node('p', human(monitor.last_result), 'inline-note'));
  const ready = Boolean(monitor.remote_id) && monitor.secret_available !== false && !['creation_uncertain', 'secret_unavailable', 'creating'].includes(monitor.status);
  if (monitor.last_triggered_at) append(panel, node('p', `Check requested ${date(monitor.last_triggered_at, true)}. Completion is recorded only after a verified delivery.`, 'status-line'));
  if (!recording) {
    if (['creating', 'creation_uncertain', 'secret_unavailable'].includes(monitor.status)) append(panel, button('Reconcile monitor', () => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor/reconcile`), { write: true }));
    append(panel, append(node('div', null, 'button-row'), button('Check now', () => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor/trigger`, {}, `Run a new expansion check for ${account.name}. This is a chargeable Monitor run, not a refresh of saved state.`), { write: true, disabled: !ready, primary: true }), button('Enable daily checks', () => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor/schedule`, { enabled: true }, `Enable recurring daily paid Monitor runs for ${account.name}. Charges can continue until you pause the monitor. Your receiver must remain available.`), { write: true, disabled: !ready }), button('Pause monitor', () => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor/schedule`, { enabled: false }), { write: true, disabled: !monitor.remote_id })));
    const form = node('form', null, 'monitor-form');
    const input = node('input'); input.type = 'url'; input.required = true; input.value = config.receiver_url || ''; input.placeholder = 'https://your-receiver.example';
    const submit = node('button', 'Update webhook URL'); submit.type = 'submit'; submit.dataset.write = 'true'; submit.disabled = !ready;
    append(form, append(node('label', 'Receiver URL after tunnel restart'), input), submit);
    form.addEventListener('submit', (event) => { event.preventDefault(); perform(() => mutation(`/api/accounts/${encodeURIComponent(account.id)}/monitor/webhook`, { receiver_url: input.value }, null, 'PATCH'), submit); });
    panel.append(append(node('details'), node('summary', 'Webhook'), form));
    append(panel, node('hr', null, 'section-rule'), button('Refresh monitor runs', async () => {
      const result = await api(`/api/accounts/${encodeURIComponent(account.id)}/monitor/runs`);
      const output = node('div', null, 'record');
      append(output, rawDetails('Monitor runs response', result));
      const runs = list(result.data || result.runs || result.results);
      runs.forEach((run) => {
        if (!run.id) return;
        append(output, button(`Inspect run ${run.id}`, async () => {
          const value = await api(`/api/accounts/${encodeURIComponent(account.id)}/monitor/runs/${encodeURIComponent(run.id)}`);
          append(output, rawDetails('Monitor output.results / output.content / output.grounding', value));
        }, { write: true }));
      });
      panel.append(output);
    }, { write: true, disabled: !ready }));
  }
  append(panel, rawDetails('Saved monitor configuration', monitor));
}
function renderCandidates() {
  const container = $('candidate-list');
  const expanded = new Set([...container.querySelectorAll('details[open]')].map((record) => record.dataset.id));
  const existing = new Set([...container.querySelectorAll('.candidate-record')].map((record) => record.dataset.id));
  container.replaceChildren();
  if (!state.candidates.length) append(container, node('p', 'No candidates', 'empty-state'));
  const latest = [...state.sessions].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
  if (latest) append(container, node('p', `Latest discovery: ${latest.geography || 'Texas'} / ${human(latest.status)} · ${date(latest.created_at, true)}`, 'drawer-intro'));
  const candidates = state.candidates.filter((candidate) => !latest || candidate.session_id === latest.id).slice(0, 10);
  $('candidate-count').textContent = candidates.length;
  candidates.forEach((candidate) => {
    const record = append(node('details', null, 'candidate-record'), node('summary', `${candidate.name || 'Unnamed candidate'} / ${candidate.selected ? 'Added' : candidate.domain_verified ? 'Confirmed' : 'Unverified'}`));
    record.dataset.id = candidate.id;
    record.open = expanded.has(candidate.id) || (!existing.has(candidate.id) && !candidate.selected);
    append(record, node('span', text(candidate.domain, 'Official domain not confirmed'), 'domain'));
    const evidence = state.evidence.filter((item) => list(candidate.evidence_ids).includes(item.id));
    evidence.forEach((item) => append(record, sourceLink(item.url, text(item.title, 'Open source')), research(item.highlights, 'No excerpt returned.', 'muted')));
    if (!recording) {
      append(record, node('p', 'Source domain only. Research will check the operating entity.', 'status-line'), append(node('div', null, 'button-row'), button('Add to research', async () => {
        if (selected.size >= 5) throw new Error('Five accounts are already selected. Research them or uncheck an account before adding another.');
        const result = await mutation(`/api/candidates/${encodeURIComponent(candidate.id)}/select`);
        if (result?.account && selected.size < 5) { selected.add(result.account.id); selectedId = result.account.id; }
        render(true);
        if (result) {
          document.querySelector('.ledger').open = true;
          notice(`${candidate.name} added and selected. Choose Research selected to confirm the paid action.`);
          $('review-selection').textContent = `Continue to Research selected (${selected.size})`;
        }
      }, { write: true, disabled: candidate.selected, primary: true })));
      if (!candidate.selected) {
        const correction = append(node('details'), node('summary', 'Correct company domain (optional)'));
        const domain = node('input'); domain.value = candidate.domain || ''; domain.setAttribute('aria-label', `Correct domain for ${candidate.name}`);
        append(correction, domain, button('Save domain correction', () => mutation(`/api/candidates/${encodeURIComponent(candidate.id)}/confirm`, { domain: domain.value }), { write: true }));
        record.append(correction);
      }
    }
    container.append(record);
  });
}
async function openInspector(accountId = null) {
  const artifacts = accountId ? state.artifacts.filter((a) => a.account_id === accountId) : state.artifacts;
  const select = $('artifact-select'); select.replaceChildren();
  artifacts.forEach((artifact) => {
    const option = node('option', `${human(artifact.operation)} · ${date(artifact.started_at, true)} · ${artifact.id}`);
    option.value = artifact.id; select.append(option);
  });
  if (!artifacts.length) { const option = node('option', 'No saved requests'); select.append(option); }
  activeArtifact = artifacts[0] || null;
  inspectorTab = 'Request';
  renderInspector();
  $('inspector-dialog').showModal();
  if (activeArtifact && !recording) await loadArtifact(activeArtifact.id);
}
async function loadArtifact(id) {
  activeArtifact = state.artifacts.find((a) => a.id === id) || null;
  renderInspector();
  if (!recording && activeArtifact) {
    const version = epoch;
    const value = await api(`/api/artifacts/${encodeURIComponent(id)}`);
    if (version === epoch && $('artifact-select').value === id) { activeArtifact = value; renderInspector(); }
  }
}
function renderInspector() {
  const artifact = activeArtifact;
  $('artifact-meta').replaceChildren();
  if (artifact) append($('artifact-meta'), metadata([['Operation', human(artifact.operation)], ['Request ID', artifact.request_id || artifact.id], ['Started', date(artifact.started_at, true)], ['Finished', date(artifact.ended_at, true)], ['Attempt', artifact.attempt], ['Reported cost', costDisplay(artifact.cost_dollars)]]));
  tabs('inspector-tabs', inspectorTabs, inspectorTab, (tab) => { inspectorTab = tab; renderInspector(); }, 'inspector-content');
  const panel = $('inspector-content'); panel.replaceChildren();
  if (!artifact) { append(panel, node('p', 'No saved requests', 'muted')); return; }
  if (artifact.error) append(panel, node('p', text(artifact.error), 'inline-note error-note'));
  const response = artifact.response;
  const grounding = response?.output?.grounding ?? null;
  const activity = { operation: artifact.operation, started_at: artifact.started_at, ended_at: artifact.ended_at, attempt: artifact.attempt, error: artifact.error ?? null, usage: artifact.usage ?? null, cost_dollars: artifact.cost_dollars ?? 'Not reported', events: response?.events ?? state.agent_runs.filter((r) => r.account_id === artifact.account_id).map((r) => ({ run_id: r.id, events: r.events ?? null, events_error: r.events_error ?? null })) };
  const value = { Request: artifact.request, Response: response, Grounding: grounding, Activity: activity }[inspectorTab];
  append(panel, node('pre', value == null ? 'Not reported' : pretty(value)));
}
function download(value, name, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([typeof value === 'string' ? value : pretty(value)], { type }));
  const anchor = node('a'); anchor.href = url; anchor.download = name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function exportData(format) {
  if (recording) {
    if (format === 'json') download(recording, 'account-watch-recording.json');
    else {
      const fields = ['id', 'name', 'domain', 'geography', 'accounts', 'qualifications', 'window_days', 'fit', 'fit_explanation', 'window_start', 'window_end', 'last_researched', 'source_references'];
      const cell = (value) => { let s = typeof value === 'object' && value !== null ? pretty(value) : text(value, ''); if (/^[=+@\-\t\r]/.test(s)) s = `'${s}`; return `"${s.replaceAll('"', '""')}"`; };
      const rows = state.accounts.map((a) => {
        const values = { ...a, geography: a.geography || 'Texas', accounts: a.accounts || 'Regional fitness chains', qualifications: a.qualifications || '2+ operating locations and expansion', window_days: a.window_days || 90 };
        return fields.map((key) => cell(key === 'source_references' ? state.evidence.filter((e) => e.account_id === a.id).map((e) => e.url).filter(Boolean).join(' | ') : values[key])).join(',');
      });
      download([fields.join(','), ...rows].join('\r\n'), 'account-watch-recording.csv', 'text/csv');
    }
    return;
  }
  const response = await fetch(`/api/export/${format}`);
  if (!response.ok) throw new Error(`Export failed (${response.status}).`);
  download(await response.text(), `account-watch.${format}`, format === 'csv' ? 'text/csv' : 'application/json');
}
async function openRecording(file) {
  if (!file) return;
  if (file.size > 20_000_000) throw new Error('Recording exceeds the 20 MB import limit.');
  const parsed = JSON.parse(await file.text());
  const incoming = parsed.recording || parsed;
  if (incoming.format !== 'account-watch' || !incoming.records || !Array.isArray(incoming.records.accounts)) throw new Error('Not an Account Watch recording. Expected format account-watch and records.accounts.');
  epoch += 1; stateController?.abort();
  const opened = recording ? { recording: incoming } : await api('/api/recordings/open', { method: 'POST', body: { recording: incoming } });
  recording = opened.recording;
  recordingId = opened.id || null;
  state = normalizeState(recording.records);
  selected.clear(); selectedId = null; detailCache = null; detailSignature = ''; activeArtifact = null;
  document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  $('recording-banner').hidden = false;
  $('recording-label').textContent = recording.label === 'Synthetic test' || recording.synthetic ? 'Synthetic test' : 'Recorded session';
  $('recording-date').textContent = `Original date: ${date(recording.recorded_at, true)}`;
  $('connection').textContent = 'Recorded / offline'; $('connection').classList.remove('offline');
  notice('Recording opened.');
  render(true);
}

document.querySelectorAll('[data-close]').forEach((element) => element.addEventListener('click', () => $(element.dataset.close).close()));
$('dismiss-error').addEventListener('click', () => { $('error-banner').hidden = true; });
$('account-search').addEventListener('input', renderAccounts);
$('fit-filter').addEventListener('change', renderAccounts);
$('candidates-button').addEventListener('click', () => { renderCandidates(); $('candidates-dialog').showModal(); });
$('review-selection').addEventListener('click', () => {
  $('candidates-dialog').close();
  document.querySelector('.ledger').open = true;
  $('research').scrollIntoView({ block: 'center' });
  $('research').focus();
});
$('discover').addEventListener('click', () => perform(async () => {
  const criteria = criteriaInput();
  if (!criteria) return;
  const result = await mutation('/api/discovery', criteria, `Discover up to ten candidates using at most two paid Exa Search requests. Geography: ${criteria.geography}. Accounts: ${criteria.accounts}. Qualifications: ${criteria.qualifications}. Window: ${criteria.window_start ? `${criteria.window_start} through ${criteria.window_end}` : `${criteria.window_days} days`}. No research or monitors start automatically.`);
  if (result) { notice('Discovery started.'); $('candidates-dialog').showModal(); }
}, $('discover')));
$('criteria-window-mode').addEventListener('change', windowMode);
$('preview-search').addEventListener('click', () => perform(async () => {
  const criteria = criteriaInput();
  if (!criteria) return;
  const value = await api('/api/discovery/preview', { method: 'POST', body: criteria });
  const panel = $('search-preview'); panel.hidden = false; panel.replaceChildren();
  value.requests.forEach((request, i) => append(panel, node('h3', i ? '2. Expansion evidence' : '1. Company discovery'), node('p', request.query, 'query-preview'), rawDetails('Exact Search payload', request)));
}, $('preview-search')));
$('criteria-form').addEventListener('submit', (event) => { event.preventDefault(); $('discover').click(); });
$('research').addEventListener('click', () => perform(async () => {
  const ids = [...selected];
  const existing = state.agent_runs.filter((run) => ids.includes(run.account_id));
  if (existing.length) {
    await selectAccount(existing[0].account_id);
    $('research-progress').scrollIntoView({ block: 'center' });
    throw new Error('A selected account already has saved research. See Research progress and refresh its saved run instead of starting a duplicate paid run.');
  }
  const result = await mutation('/api/research', { account_ids: [...selected], web_only: false }, `Research ${selected.size} selected account${selected.size === 1 ? '' : 's'} using each account's saved geography: one location Search and one Exa Agent run per account, with Fiber requested. Up to two accounts run concurrently. Agent and partner usage may add costs.`);
  if (result) {
    ids.forEach((id) => selected.delete(id));
    await selectAccount(ids[0]);
    notice(`${result.runs.length} research run(s) queued. Follow Research progress below; open the company for Agent criteria and sources.`);
    $('research-progress').scrollIntoView({ block: 'center' });
  }
}, $('research')));
document.querySelectorAll('[data-stage]').forEach((control) => control.addEventListener('click', () => perform(async () => {
  const stage = control.dataset.stage;
  document.querySelectorAll('[data-stage]').forEach((item) => item.setAttribute('aria-current', item === control ? 'step' : 'false'));
  if (stage === 'criteria') { $('criteria-panel').open = true; $('criteria-panel').scrollIntoView({ block: 'start' }); return; }
  document.querySelector('.ledger').open = true;
  if (stage === 'accounts') { $('workspace').scrollIntoView({ block: 'start' }); return; }
  const id = selectedId || [...selected][0] || state.accounts[0]?.id;
  if (!id) { notice('Discover companies and Add to research first. The account sections become available after adding a company.'); return; }
  await selectAccount(id);
  activeTab = stage === 'review' ? 'Evidence' : 'Overview'; renderDetail();
  $('account-detail').scrollIntoView({ block: 'start' });
}, control)));
$('inspect-button').addEventListener('click', () => perform(() => openInspector()));
$('artifact-select').addEventListener('change', () => perform(() => loadArtifact($('artifact-select').value)));
async function openSettings() {
  config = await api('/api/config');
  $('key-status').textContent = config.api_key_configured ? 'Exa key configured on the server.' : 'Exa key not configured. Load exa_key server-side to enable live research.';
  $('receiver-url').value = config.receiver_url || '';
  $('settings-dialog').showModal();
}
$('settings-button').addEventListener('click', () => perform(openSettings));
$('settings-form').addEventListener('submit', (event) => {
  event.preventDefault();
  perform(async () => {
    config = await api('/api/config', { method: 'PATCH', body: { receiver_url: $('receiver-url').value } });
    $('settings-dialog').close(); render(true); notice('Configuration saved. Existing monitor webhook URLs must be updated separately in the Monitor tab.');
  });
});
$('save-recording').addEventListener('click', () => perform(async () => {
  const result = await api('/api/recordings/save', { method: 'POST', body: {} });
  download(result.recording, `account-watch-${new Date().toISOString().slice(0, 10)}.json`);
  notice('Recording saved.');
}, $('save-recording')));
$('open-recording').addEventListener('click', () => $('recording-file').click());
$('recording-file').addEventListener('change', () => perform(async () => {
  try { await openRecording($('recording-file').files[0]); } finally { $('recording-file').value = ''; }
}));
$('exit-recording').addEventListener('click', () => perform(async () => {
  recording = null; recordingId = null; epoch += 1; state = normalizeState({}); selectedId = null; detailCache = null;
  $('recording-banner').hidden = true;
  render(true);
  notice('Live workspace.');
  config = await api('/api/config');
  await refresh(true);
}));
$('export-json').addEventListener('click', () => perform(() => exportData('json')));
$('export-csv').addEventListener('click', () => perform(() => exportData('csv')));

async function pollState() {
  if (!recording && !document.hidden && !busy) await refresh();
  setTimeout(pollState, 4000);
}
async function initialize() {
  try { config = await api('/api/config'); } catch (issue) { error(issue); }
  await refresh(true);
  setTimeout(pollState, 4000);
}
initialize();
