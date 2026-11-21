'use strict';

const root = document.getElementById('console');
const notice = element('div');
const view = element('div');
root.append(notice, view);

const state = {
  csrf: null,
  signedOut: false,
  lost: false,
  sessionRechecked: false,
  scope: 'machine',
  itemId: null,
  snapshot: null,
  loading: false,
  sending: false,
  draft: '',
  message: '',
  request: 0,
};
let viewKey = '';
let noticeKey = '';
let activeRead = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action, className) {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

async function request(path, options) {
  let response, body;
  try {
    response = await fetch(path, {
      credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(12_000), ...options,
    });
    body = await response.json();
  } catch {
    throw { kind: 'network' };
  }
  if (response.status === 403 && body?.error === 'refused') {
    throw { kind: 'refused', status: 403, error: body.error };
  }
  if (!response.ok || body?.error) {
    throw { kind: 'typed', status: response.status, error: body?.error };
  }
  return body;
}

const api = {
  async read(operation, args = {}) {
    const query = Object.entries(args).map(([key, value]) => {
      // The listener accepts literal ASCII query atoms, without decoding aliases.
      if (typeof value !== 'string' || !/^[A-Za-z0-9_.:~-]+$/.test(value)) {
        throw { kind: 'typed', error: { message: 'The selected question or project has an invalid identifier.' } };
      }
      return key + '=' + value;
    }).join('&');
    return (await request('/api/read/' + operation + (query ? '?' + query : ''))).result;
  },
  async act(operation, args) {
    if (!state.csrf || state.signedOut || state.lost) return;
    const body = await request('/api/op/' + operation, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-ADT-CSRF': state.csrf },
      body: JSON.stringify(args),
    });
    return body.result;
  },
};

async function session(launch) {
  const options = launch ? {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ launch }),
  } : undefined;
  const result = await request('/api/session', options);
  if (typeof result?.csrf_token !== 'string' || !result.csrf_token) throw { kind: 'network' };
  state.csrf = result.csrf_token;
}

function signOut() {
  state.csrf = null;
  state.signedOut = true;
  state.snapshot = null;
  state.draft = '';
  state.message = '';
  state.lost = false;
  poll.stop();
  render();
}

async function recheckSession() {
  if (state.sessionRechecked) return false;
  state.sessionRechecked = true;
  state.csrf = null;
  try {
    await session();
    return true;
  } catch (failure) {
    if (failure.kind === 'refused' || failure.kind === 'typed') signOut();
    return false;
  }
}

function formatTime(value, form) {
  let date;
  if (form === 'ms' && /^(?:0|[1-9][0-9]{0,14})$/.test(value)) {
    date = new Date(Number(value));
  } else if (form === 'stop' && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(value)) {
    date = new Date(value);
    if (Number.isNaN(date.getTime()) || date.toISOString() !== value.replace('Z', '.000Z')) return value;
  } else {
    return value;
  }
  if (Number.isNaN(date.getTime())) return value;
  const pad = number => String(number).padStart(2, '0');
  return String(date.getFullYear()).padStart(4, '0') + '-' + pad(date.getMonth() + 1) +
    '-' + pad(date.getDate()) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

function parsed(value) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function messageLines(item) {
  const texts = parsed(item.texts);
  if (typeof texts?.subject === 'string' && typeof texts?.body === 'string') {
    return [texts.subject, texts.body];
  }
  return [item.texts ?? ''];
}

function errorText(error) {
  const fields = [error?.code, error?.message ?? error?.text, error?.cause?.kind];
  return fields.filter(value => typeof value === 'string' && value).join(' · ') ||
    'The engine could not complete the request.';
}

function canAct() {
  return Boolean(state.csrf && !state.lost && !state.loading && !state.sending);
}

function copyButton(label, text) {
  const copy = button(label, async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = 'Copied';
    } catch {
      copy.textContent = 'Copy unavailable';
    }
  });
  return copy;
}

function commandLine(text, command, suffix = '') {
  const line = element('p', undefined, 'status warning');
  line.setAttribute('role', 'status');
  line.append(text, element('code', command), suffix, copyButton('Copy command', command));
  return line;
}

function renderNotice() {
  const key = JSON.stringify([state.signedOut, state.lost, Boolean(state.itemId), state.message]);
  if (noticeKey === key) return;
  noticeKey = key;
  notice.replaceChildren();
  if (state.signedOut) {
    notice.append(commandLine('This page is not signed in. To open it, run ', 'adt console open', ' in a terminal.'));
  } else if (state.lost) {
    const target = state.itemId ? 'page' : 'list';
    notice.append(commandLine('Cannot reach the engine, so this ' + target + ' may be out of date. Run ', 'adt engine status', '.'));
  }
  if (state.message) {
    const line = element('p', state.message, 'status warning');
    line.setAttribute('role', 'status');
    notice.append(line);
  }
}

function metadata(item) {
  const parts = [element('span', 'Question', 'kind'), element('span', item.project_id ?? 'all projects')];
  const asker = item.actor_address ?? item.answer_to;
  if (asker) parts.push(element('span', 'from ' + asker));
  parts.push(element('time', formatTime(item.raised_at_ms, 'ms')));
  const line = element('p', undefined, 'meta');
  line.append(...parts);
  return line;
}

function listView(snapshot) {
  const section = element('section');
  const toolbar = element('div', undefined, 'toolbar');
  const heading = element('h1', 'Waiting for you · ' + snapshot.items.length);
  const select = element('select');
  const label = element('label', 'Project');
  label.append(select);
  const scopes = [['machine', 'All projects'], ...snapshot.projects.map(project => [project, project])];
  for (const [scope, name] of scopes) {
    const option = element('option', name);
    option.value = scope;
    select.append(option);
  }
  select.value = state.scope;
  select.addEventListener('change', () => {
    if (canAct()) views.show('list', { scope: select.value });
  });
  toolbar.append(heading, label);
  section.append(toolbar);
  if (snapshot.items.length === 0) section.append(element('p', 'No pending questions.'));
  for (const item of snapshot.items) {
    const lines = messageLines(item);
    const row = element('article', undefined, 'question');
    const title = lines[0] || 'Question';
    const canReply = Boolean(item.answer_to);
    const open = button(canReply ? 'Answer' : 'Open', () => {
      if (canAct()) views.show('item', { itemId: item.item_id });
    }, canReply ? 'primary' : undefined);
    open.setAttribute('aria-label', open.textContent + ': ' + title);
    row.append(metadata(item), element('h2', title), element('p', lines.slice(1).join('\n'), 'message'), open);
    section.append(row);
  }
  return section;
}

function answerForm(item) {
  const form = element('form');
  const choices = parsed(item.choices);
  if (Array.isArray(choices) && choices.some(choice => typeof choice === 'string' && choice)) {
    const offered = element('div', undefined, 'actions choices');
    for (const choice of choices) {
      if (typeof choice === 'string' && choice) {
        offered.append(button(choice, () => sendAnswer({ choice })));
      }
    }
    form.append(element('p', 'Its suggested answers'), offered);
  }
  const label = element('label', 'Your answer');
  label.htmlFor = 'answer-text';
  const textarea = element('textarea');
  textarea.id = 'answer-text';
  textarea.value = state.draft;
  textarea.required = true;
  textarea.addEventListener('input', () => { state.draft = textarea.value; });
  const submit = element('button', 'Send answer', 'primary');
  submit.type = 'submit';
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (textarea.value.trim()) sendAnswer({ text: textarea.value });
  });
  const actions = element('div', undefined, 'actions');
  actions.append(submit, element('span', 'It goes to ' + item.answer_to + ' as your reply.', 'hint'));
  form.append(label, textarea, actions);
  return form;
}

function answerStatus(item) {
  if (item.state === 'applying') return 'Your answer is recorded and being delivered.';
  if (item.state === 'resolved') return 'Answer delivered.';
  const outcome = parsed(item.outcome);
  const reason = outcome?.code ?? item.job?.last_reason;
  const detail = reason ? ' ' + reason : '';
  if (item.state === 'refused') return 'Answer delivery refused.' + detail;
  if (item.state === 'faulted') return 'Answer delivery faulted.' + detail;
  return '';
}

function detailView({ item, events }) {
  const section = element('section', undefined, 'detail');
  const back = button('Back to the list', () => {
    if (canAct()) views.show('list');
  }, 'back');
  const lines = messageLines(item);
  section.append(back, metadata(item), element('h1', lines[0] || 'Question'),
    element('p', lines.slice(1).join('\n'), 'message'));
  const status = answerStatus(item);
  if (status) {
    const warning = item.state === 'refused' || item.state === 'faulted';
    const line = element('p', status, warning ? 'status warning' : 'status');
    line.setAttribute('role', 'status');
    section.append(line);
  }
  if (typeof item.answer?.answer === 'string') {
    const recorded = parsed(item.answer.answer);
    const parts = [recorded?.choice, recorded?.text].filter(part => typeof part === 'string' && part);
    section.append(element('h2', 'Recorded answer'),
      element('p', parts.length ? parts.join('\n') : item.answer.answer, 'message'));
  }
  if (item.state !== 'pending' && state.draft) {
    const draft = state.draft;
    section.append(element('h2', 'Your draft'), element('p', draft, 'message'), copyButton('Copy my text', draft));
  }
  if (item.state === 'pending' && item.answer_to) section.append(answerForm(item));
  if (item.state === 'pending' && !item.answer_to) {
    section.append(element('p', 'No answer can be sent: the agent that asked this has left its team.', 'hint'));
  }
  if (events.length) {
    const history = element('ol', undefined, 'history');
    for (const event of events) {
      const detail = parsed(event.detail);
      const line = element('li', formatTime(event.at_ms, 'ms') + ' · ' + event.event_kind);
      const eventText = detail?.code ?? detail?.kind;
      if (typeof eventText === 'string') line.append(' · ', element('span', eventText));
      history.append(line);
    }
    section.append(element('h2', 'History'), history);
  }
  return section;
}

function render() {
  renderNotice();
  const snapshot = state.snapshot;
  const key = JSON.stringify([state.signedOut, state.scope, snapshot]);
  if (key !== viewKey) {
    viewKey = key;
    const focusedAnswer = document.activeElement?.id === 'answer-text';
    const selection = focusedAnswer ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
    if (state.signedOut) view.replaceChildren();
    else if (!snapshot) view.replaceChildren(element('h1', 'Operator Console'));
    else view.replaceChildren(snapshot.item ? detailView(snapshot) : listView(snapshot));
    const textarea = document.getElementById('answer-text');
    if (selection && textarea) {
      textarea.focus();
      textarea.setSelectionRange(...selection);
    }
  }
  for (const control of view.querySelectorAll('button, select')) control.disabled = !canAct();
  const textarea = document.getElementById('answer-text');
  if (textarea) textarea.readOnly = !canAct();
}

async function pages(operation, args, cursor, collect) {
  const values = [];
  const seen = new Set();
  let page = await api.read(operation, args);
  const first = page;
  for (;;) {
    values.push(...page[collect]);
    const next = page.continuation;
    if (!next) return { first, values };
    if (seen.has(next)) throw { kind: 'typed', error: { message: 'The engine repeated a page of results.' } };
    seen.add(next);
    page = await api.read(operation, { ...args, [cursor]: next });
  }
}

async function refresh() {
  if (state.signedOut || state.sending) return;
  const requestId = ++state.request;
  activeRead = requestId;
  const { itemId, scope } = state;
  try {
    if (!state.csrf) await session();
    const list = await pages('queue.list', { scope, state: 'pending', kind: 'question', page_size: '100' },
      'continuation', 'items');
    const items = list.values.filter(item => item.kind === 'question' && item.state === 'pending')
      .sort((a, b) => Number(b.raised_position) - Number(a.raised_position));
    const projects = scope === 'machine'
      ? [...new Set(items.map(item => item.project_id).filter(Boolean))].sort()
      : state.snapshot?.projects ?? [scope];
    let next = { items, projects };
    if (itemId) {
      try {
        const detail = await pages('queue.get', { item_id: itemId, history_page_size: '100' }, 'history_continuation', 'events');
        if (detail.first.item.kind !== 'question') throw { kind: 'missing' };
        next = { ...next, item: detail.first.item, events: detail.values };
      } catch (failure) {
        if (failure.kind !== 'missing' && failure.error?.cause?.kind !== 'item_not_found') throw failure;
        if (requestId !== state.request) return;
        state.itemId = null;
        state.message = 'That question is no longer available.';
      }
    }
    if (requestId !== state.request) return;
    state.snapshot = next;
    state.sessionRechecked = false;
    state.lost = false;
  } catch (failure) {
    if (requestId !== state.request) return;
    state.lost = true;
    render();
    if (failure.kind === 'refused') {
      if (!state.csrf) signOut();
      else await recheckSession();
    } else if (failure.kind === 'typed') {
      state.message = errorText(failure.error);
    }
    if (requestId !== state.request) return;
  } finally {
    if (activeRead === requestId) activeRead = null;
    if (requestId === state.request) {
      state.loading = false;
      render();
    }
  }
}

const views = {
  async show(name, params = {}) {
    const previousId = state.itemId;
    state.itemId = name === 'item' ? params.itemId : null;
    state.scope = params.scope ?? state.scope;
    if (previousId !== state.itemId) state.draft = '';
    state.message = '';
    state.loading = true;
    render();
    await refresh();
    if (!state.loading) {
      const heading = view.querySelector('h1');
      if (heading) {
        heading.tabIndex = -1;
        heading.focus();
      }
    }
  },
};

let timer;
function pollWhenVisible() {
  if (!document.hidden && !state.sending && !state.loading && activeRead === null) refresh();
}
const poll = {
  start() {
    timer = setInterval(pollWhenVisible, 5000);
    document.addEventListener('visibilitychange', pollWhenVisible);
  },
  stop() {
    clearInterval(timer);
    document.removeEventListener('visibilitychange', pollWhenVisible);
  },
};

async function sendAnswer(answer) {
  const item = state.snapshot?.item;
  if (!canAct() || item?.kind !== 'question' || item.state !== 'pending' || !item.answer_to) return;
  state.sending = true;
  ++state.request; // A read started before this action cannot replace its result.
  state.message = '';
  render();
  try {
    const result = await api.act('queue.answer', {
      answer,
      expected_revision: item.revision,
      item_id: item.item_id,
      operation_key: crypto.randomUUID().replaceAll('-', ''),
    });
    state.draft = '';
    state.snapshot = { ...state.snapshot, item: { ...item, state: result.state, revision: result.revision } };
  } catch (failure) {
    if (failure.kind === 'refused') {
      state.message = 'The engine refused the request.';
      if (!await recheckSession() && !state.signedOut) state.lost = true;
    } else if (failure.kind === 'typed') {
      state.message = errorText(failure.error);
    } else {
      state.message = 'Could not tell whether your answer was recorded. Checking the question again.';
      state.lost = true;
    }
  } finally {
    state.sending = false;
    state.loading = !state.signedOut;
    render();
    await refresh();
  }
}

async function start() {
  const params = new URLSearchParams(location.hash.slice(1));
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const itemId = params.get('item');
  if (itemId && !/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(itemId)) state.message = 'That question is no longer available.';
  else state.itemId = itemId;
  try {
    await session(params.get('launch'));
  } catch (failure) {
    if (failure.kind !== 'network') {
      signOut();
      return;
    }
    state.lost = true;
    render();
  }
  poll.start();
  if (state.csrf && !document.hidden) await refresh();
}
start();
