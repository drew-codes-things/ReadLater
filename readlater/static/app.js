'use strict';

const VIEW_NAMES = { inbox: 'My list', favorites: 'Favourites', archive: 'Archive', all: 'Everything', broken: 'Broken links' };
const EMPTY_TEXT = {
  inbox: 'Nothing saved yet. Use Save a link, the bookmarklet, or share a page here.',
  favorites: 'Favourites appear here.',
  archive: 'Archived links appear here.',
  all: 'Nothing saved yet.',
  broken: 'No broken links found. Check links looks for pages that are gone.',
  search: 'Nothing matches that search.',
  scoped: 'Nothing here yet.',
};
const PAGE_SIZE = 50;
const SEARCH_DELAY_MS = 300;
const TOAST_MS = 3500;
const NOTE_DELAY_MS = 800;
const PROGRESS_DELAY_MS = 1000;
const PROGRESS_STEP = 0.01;
const CHECK_POLL_MS = 1500;
const NARROW_QUERY = '(max-width: 1100px)';
const PHONE_QUERY = '(max-width: 760px)';
const WEB_ADDRESS = /https?:\/\/[^\s<>"']+/i;
const NO_COLLECTION = '';

const state = {
  csrf: '', view: 'inbox', collectionId: null, tag: '', query: '',
  collections: [], tags: [], counts: { inbox: 0, favorites: 0, archive: 0, all: 0, broken: 0 },
  items: [], next: null, currentId: null, current: null, editingCollection: null, savedProgress: 0,
};
const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
const narrowScreen = window.matchMedia(NARROW_QUERY);
const phoneScreen = window.matchMedia(PHONE_QUERY);

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function api(method, path, body, contentType) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  let payload;
  if (contentType) {
    headers['Content-Type'] = contentType;
    payload = body;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const message = data && data.error ? data.error : `the server answered ${response.status}`;
    throw new ApiError(message, response.status, data);
  }
  return data;
}

let toastTimer = 0;
function showToast(message, isError = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}

function reportError(error) {
  if (error instanceof ApiError && error.status === 401) {
    showToast('You are signed out. Reload the page to sign in again.', true);
    return;
  }
  showToast(error.message || String(error), true);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function parseTags(text) {
  return text.split(',').map((tag) => tag.trim()).filter(Boolean);
}

function minutesText(minutes) {
  if (!minutes) return '';
  return minutes === 1 ? '1 min read' : `${minutes} min read`;
}

function collectionById(id) {
  return state.collections.find((collection) => collection.id === id) || null;
}

function hasScope() {
  return state.collectionId !== null || state.tag !== '';
}

function scopeTitle() {
  if (state.query) return `Search: ${state.query}`;
  if (state.collectionId !== null) return (collectionById(state.collectionId) || { name: 'Collection' }).name;
  if (state.tag) return `#${state.tag}`;
  return VIEW_NAMES[state.view];
}

function listView() {
  return state.query || hasScope() ? 'all' : state.view;
}

function scopeParams() {
  const params = new URLSearchParams({ view: listView(), limit: String(PAGE_SIZE) });
  if (state.collectionId !== null) params.set('collection', String(state.collectionId));
  if (state.tag) params.set('tag', state.tag);
  if (state.query) params.set('q', state.query);
  return params;
}

function setPane(pane) {
  document.body.dataset.pane = pane;
  byId('menu-btn').setAttribute('aria-expanded', String(pane === 'nav'));
}

function navButton(label, count, isCurrent, onClick) {
  const button = element('button', 'nav-btn');
  button.type = 'button';
  button.append(element('span', 'label', label));
  if (count) button.append(element('span', 'count', String(count)));
  button.setAttribute('aria-current', String(isCurrent));
  button.addEventListener('click', onClick);
  return button;
}

function renderSidebar() {
  const noFilter = !state.query && !hasScope();
  for (const button of document.querySelectorAll('.views .nav-btn')) {
    button.setAttribute('aria-current', String(noFilter && button.dataset.view === state.view));
  }
  for (const [view, count] of Object.entries(state.counts)) {
    const badge = byId(`count-${view}`);
    if (badge) badge.textContent = count ? String(count) : '';
  }
  const collections = byId('collection-list');
  collections.replaceChildren(...state.collections.map((collection) => navButton(
    collection.name, collection.count, state.collectionId === collection.id && !state.query,
    () => selectScope({ collectionId: collection.id }),
  )));
  if (!state.collections.length) collections.append(element('p', 'hint', 'No collections yet.'));
  const tags = byId('tag-list');
  tags.replaceChildren(...state.tags.map((tag) => {
    const button = element('button', 'tag-btn', `${tag.name} ${tag.count}`);
    button.type = 'button';
    button.setAttribute('aria-current', String(state.tag === tag.name && !state.query));
    button.addEventListener('click', () => selectScope({ tag: tag.name }));
    return button;
  }));
  if (!state.tags.length) tags.append(element('p', 'hint', 'Tags you add appear here.'));
}

function fillCollectionSelect(select, selected) {
  select.replaceChildren();
  const none = element('option', '', 'No collection');
  none.value = NO_COLLECTION;
  select.append(none);
  for (const collection of state.collections) {
    const option = element('option', '', collection.name);
    option.value = String(collection.id);
    select.append(option);
  }
  select.value = selected === null || selected === undefined ? NO_COLLECTION : String(selected);
}

function selectedCollectionId(select) {
  return select.value === NO_COLLECTION ? null : Number(select.value);
}

async function loadLibrary() {
  const data = await api('GET', '/api/library');
  state.collections = data.collections;
  state.tags = data.tags;
  state.counts = data.counts;
  renderSidebar();
  renderListHead();
}

function renderListHead() {
  byId('list-title').textContent = scopeTitle();
  byId('scope-settings').hidden = state.query !== '' || state.collectionId === null;
}

function itemNode(item) {
  const node = element('li', 'item');
  node.tabIndex = 0;
  node.dataset.id = String(item.id);
  node.classList.toggle('archived', item.archived);
  node.setAttribute('aria-current', String(item.id === state.currentId));
  const top = element('div', 'item-top');
  top.append(element('span', 'item-site', item.siteName));
  if (item.favorite) top.append(element('span', 'item-star', 'Favourite'));
  top.append(element('span', '', minutesText(item.readingMinutes)));
  node.append(top, element('h3', 'item-title', item.title));
  if (item.excerpt) node.append(element('p', 'item-excerpt', item.excerpt));
  const meta = element('div', 'item-meta');
  for (const tag of item.tags) meta.append(element('span', 'item-tag', tag));
  if (item.linkStatus === 'dead') meta.append(element('span', 'item-dead', 'Link is gone'));
  if (!item.hasText) meta.append(element('span', '', 'Link only'));
  if (item.highlightCount) meta.append(element('span', '', item.highlightCount === 1 ? '1 highlight' : `${item.highlightCount} highlights`));
  if (meta.childElementCount) node.append(meta);
  if (item.progress > 0) {
    const bar = element('div', 'progress');
    const fill = element('div', 'progress-fill');
    fill.style.width = `${Math.round(item.progress * 100)}%`;
    bar.append(fill);
    bar.title = `${Math.round(item.progress * 100)}% read`;
    node.append(bar);
  }
  node.addEventListener('click', () => openItem(item.id));
  node.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') openItem(item.id);
  });
  return node;
}

function renderItems() {
  byId('item-list').replaceChildren(...state.items.map(itemNode));
  const empty = byId('empty-state');
  empty.hidden = state.items.length > 0;
  if (state.query) empty.textContent = EMPTY_TEXT.search;
  else if (hasScope()) empty.textContent = EMPTY_TEXT.scoped;
  else empty.textContent = EMPTY_TEXT[state.view];
  byId('more-btn').hidden = !state.next;
  byId('list-count').textContent = state.items.length ? `${state.items.length}${state.next ? '+' : ''}` : '';
}

async function loadItems() {
  const data = await api('GET', `/api/items?${scopeParams()}`);
  state.items = data.items;
  state.next = data.next;
  renderItems();
}

async function loadMore() {
  if (!state.next) return false;
  const params = scopeParams();
  params.set('before', state.next);
  const data = await api('GET', `/api/items?${params}`);
  state.items = state.items.concat(data.items);
  state.next = data.next;
  renderItems();
  return data.items.length > 0;
}

async function selectScope({ view = 'all', collectionId = null, tag = '' }) {
  state.view = view;
  state.collectionId = collectionId;
  state.tag = tag;
  state.query = '';
  byId('search').value = '';
  renderSidebar();
  renderListHead();
  setPane('list');
  await loadItems().catch(reportError);
  byId('item-list').parentElement.scrollTop = 0;
}

function replaceListItem(item) {
  const index = state.items.findIndex((entry) => entry.id === item.id);
  if (index === -1) return;
  state.items[index] = { ...state.items[index], ...item };
  const node = document.querySelector(`.item[data-id="${item.id}"]`);
  if (node) node.replaceWith(itemNode(state.items[index]));
}

function readerIsShowing() {
  return getComputedStyle(byId('reader')).display !== 'none' && !byId('reader-body').hidden;
}

function scrollBox() {
  return phoneScreen.matches ? document.scrollingElement : byId('reader');
}

function readProgress() {
  const box = scrollBox();
  const room = box.scrollHeight - box.clientHeight;
  if (room <= 0) return 1;
  return Math.min(1, Math.max(0, box.scrollTop / room));
}

function restoreProgress(progress) {
  const box = scrollBox();
  const room = box.scrollHeight - box.clientHeight;
  box.scrollTop = progress > 0 && room > 0 ? Math.round(progress * room) : 0;
}

let progressTimer = 0;
function onReaderScroll() {
  if (!state.current || !readerIsShowing()) return;
  clearTimeout(progressTimer);
  progressTimer = setTimeout(() => saveProgress().catch(reportError), PROGRESS_DELAY_MS);
}

async function saveProgress() {
  const item = state.current;
  if (!item || !item.hasText || !readerIsShowing()) return;
  const progress = Math.round(readProgress() * 1000) / 1000;
  if (Math.abs(progress - state.savedProgress) < PROGRESS_STEP) return;
  state.savedProgress = progress;
  const changed = await api('PATCH', `/api/items/${item.id}`, { progress });
  if (state.current && state.current.id === item.id) state.current.progress = changed.progress;
  replaceListItem({ id: item.id, progress: changed.progress });
}

function textIndex(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const positions = [];
  let squeezed = '';
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const value = node.nodeValue;
    for (let offset = 0; offset < value.length; offset += 1) {
      if (/\s/.test(value[offset])) continue;
      squeezed += value[offset];
      positions.push([node, offset]);
    }
  }
  return { squeezed, positions };
}

function segmentsFor(positions, start, length) {
  const segments = [];
  let current = null;
  for (let index = start; index < start + length; index += 1) {
    const [node, offset] = positions[index];
    if (!current || current.node !== node) {
      current = { node, from: offset, to: offset + 1 };
      segments.push(current);
    } else {
      current.to = offset + 1;
    }
  }
  return segments;
}

function markText(container, highlight) {
  const needle = highlight.text.replace(/\s+/g, '');
  if (!needle) return false;
  const { squeezed, positions } = textIndex(container);
  const start = squeezed.indexOf(needle);
  if (start < 0) return false;
  for (const segment of segmentsFor(positions, start, needle.length)) {
    const range = document.createRange();
    range.setStart(segment.node, segment.from);
    range.setEnd(segment.node, segment.to);
    const mark = element('mark', 'hl');
    mark.dataset.highlight = String(highlight.id);
    range.surroundContents(mark);
  }
  return true;
}

function paintHighlights() {
  const container = byId('article-content');
  for (const highlight of state.current.highlights) markText(container, highlight);
}

function showHighlight(id) {
  const mark = document.querySelector(`mark.hl[data-highlight="${id}"]`);
  if (!mark) return;
  mark.scrollIntoView({ block: 'center' });
  mark.classList.add('flash');
  setTimeout(() => mark.classList.remove('flash'), 1200);
}

function renderHighlightList() {
  const highlights = state.current.highlights;
  byId('highlights-empty').hidden = highlights.length > 0;
  byId('highlight-list').replaceChildren(...highlights.map((highlight) => {
    const entry = element('li', 'highlight-entry');
    const text = element('p', 'highlight-text', highlight.text);
    text.title = 'Show in the article';
    text.addEventListener('click', () => showHighlight(highlight.id));
    const remove = element('button', 'icon-btn small', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', 'Remove this highlight');
    remove.title = 'Remove this highlight';
    remove.addEventListener('click', () => removeHighlight(highlight.id).catch(reportError));
    entry.append(text, remove);
    return entry;
  }));
}

function renderReaderButtons() {
  const item = state.current;
  const favorite = byId('favorite-btn');
  favorite.setAttribute('aria-pressed', String(item.favorite));
  favorite.setAttribute('aria-label', item.favorite ? 'Remove from favourites' : 'Favourite');
  const archive = byId('archive-btn');
  archive.setAttribute('aria-pressed', String(item.archived));
  archive.setAttribute('aria-label', item.archived ? 'Move back to My list' : 'Archive');
  archive.title = item.archived ? 'Move back to My list (a)' : 'Archive (a)';
}

function metaText(item) {
  const parts = [];
  if (item.author) parts.push(item.author);
  if (item.publishedAt) parts.push(dateFormat.format(new Date(item.publishedAt)));
  if (item.readingMinutes) parts.push(minutesText(item.readingMinutes));
  parts.push(`saved ${dateFormat.format(new Date(item.createdAt))}`);
  if (item.linkStatus === 'dead') parts.push(`link is gone (${item.linkDetail})`);
  else if (item.linkStatus === 'ok') parts.push('link works');
  else if (item.linkStatus === 'error') parts.push(`link could not be checked (${item.linkDetail})`);
  return parts.join(' · ');
}

function renderReader(item) {
  byId('reader-empty').hidden = true;
  byId('reader-body').hidden = false;
  byId('reader-site').textContent = item.siteName;
  byId('reader-title').textContent = item.title;
  byId('reader-meta').textContent = metaText(item);
  byId('original-link').href = item.url;
  byId('reader-tags').value = item.tags.join(', ');
  fillCollectionSelect(byId('reader-collection'), item.collectionId);
  const fetchNotice = byId('fetch-notice');
  fetchNotice.hidden = !item.fetchError && item.hasText;
  byId('fetch-text').textContent = item.fetchError
    ? `The page could not be read: ${item.fetchError}`
    : 'Only the link is saved. Fetch the page to read it here.';
  const notice = byId('images-notice');
  notice.hidden = item.hiddenImages === 0;
  byId('images-text').textContent = item.hiddenImages === 1 ? '1 image is hidden so the site cannot track you.' : `${item.hiddenImages} images are hidden so the site cannot track you.`;
  byId('article-content').innerHTML = item.html;
  paintHighlights();
  renderHighlightList();
  byId('note').value = item.note;
  byId('note-state').textContent = '';
  renderReaderButtons();
}

async function openItem(id) {
  if (state.current && state.current.id !== id) await saveProgress().catch(reportError);
  state.currentId = id;
  for (const node of document.querySelectorAll('.item')) {
    node.setAttribute('aria-current', String(node.dataset.id === String(id)));
  }
  const node = document.querySelector(`.item[data-id="${id}"]`);
  if (node) node.scrollIntoView({ block: 'nearest' });
  if (narrowScreen.matches) setPane('reader');
  try {
    const item = await api('GET', `/api/items/${id}`);
    if (state.currentId !== id) return;
    state.current = item;
    state.savedProgress = item.progress;
    renderReader(item);
    requestAnimationFrame(() => restoreProgress(item.progress));
  } catch (error) {
    reportError(error);
  }
}

async function changeCurrent(changes) {
  const item = state.current;
  if (!item) return null;
  const changed = await api('PATCH', `/api/items/${item.id}`, changes);
  Object.assign(item, changed);
  replaceListItem(changed);
  renderReaderButtons();
  await loadLibrary();
  return changed;
}

function toggleFavorite() {
  if (state.current) changeCurrent({ favorite: !state.current.favorite }).catch(reportError);
}

async function toggleArchive() {
  if (!state.current) return;
  const changed = await changeCurrent({ archived: !state.current.archived });
  if (changed) showToast(changed.archived ? 'Archived' : 'Back in My list');
}

function selectionInArticle() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return '';
  const container = byId('article-content');
  const range = selection.getRangeAt(0);
  if (!container.contains(range.commonAncestorContainer)) return '';
  return selection.toString();
}

async function highlightSelection() {
  if (!state.current) return;
  const text = selectionInArticle().trim();
  if (!text) {
    showToast('Select some text in the article first.');
    return;
  }
  const highlight = await api('POST', `/api/items/${state.current.id}/highlights`, { text });
  window.getSelection().removeAllRanges();
  state.current.highlights.push(highlight);
  markText(byId('article-content'), highlight);
  renderHighlightList();
  replaceListItem({ id: state.current.id, highlightCount: state.current.highlights.length });
  showToast('Highlighted');
}

async function removeHighlight(id) {
  await api('DELETE', `/api/highlights/${id}`);
  const item = state.current;
  if (!item) return;
  item.highlights = item.highlights.filter((highlight) => highlight.id !== id);
  for (const mark of document.querySelectorAll(`mark.hl[data-highlight="${id}"]`)) mark.replaceWith(...mark.childNodes);
  byId('article-content').normalize();
  renderHighlightList();
  replaceListItem({ id: item.id, highlightCount: item.highlights.length });
}

let noteTimer = 0;
function onNoteInput() {
  byId('note-state').textContent = 'saving...';
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => saveNote().catch(reportError), NOTE_DELAY_MS);
}

async function saveNote() {
  const item = state.current;
  if (!item) return;
  const note = byId('note').value;
  if (note === item.note) {
    byId('note-state').textContent = 'saved';
    return;
  }
  const changed = await api('PATCH', `/api/items/${item.id}`, { note });
  item.note = changed.note;
  if (state.current === item) byId('note-state').textContent = 'saved';
}

async function saveReaderTags() {
  const item = state.current;
  if (!item) return;
  const tags = parseTags(byId('reader-tags').value);
  if (tags.join(',') === item.tags.join(',')) return;
  try {
    const changed = await changeCurrent({ tags });
    byId('reader-tags').value = changed.tags.join(', ');
  } catch (error) {
    reportError(error);
    byId('reader-tags').value = item.tags.join(', ');
  }
}

async function saveReaderCollection() {
  await changeCurrent({ collectionId: selectedCollectionId(byId('reader-collection')) }).catch(reportError);
}

async function refetchCurrent() {
  const item = state.current;
  if (!item) return;
  const button = byId('refetch-btn');
  button.disabled = true;
  try {
    const result = await api('POST', `/api/items/${item.id}/refetch`);
    showToast(result.fetchError ? `Still could not read it: ${result.fetchError}` : 'Fetched', Boolean(result.fetchError));
    replaceListItem(result);
    await openItem(item.id);
  } finally {
    button.disabled = false;
  }
}

async function checkCurrentLink() {
  const item = state.current;
  if (!item) return;
  const result = await api('POST', `/api/items/${item.id}/check`);
  Object.assign(item, { linkStatus: result.linkStatus, linkDetail: result.linkDetail });
  byId('reader-meta').textContent = metaText(item);
  replaceListItem(result);
  await loadLibrary();
  showToast(result.linkStatus === 'ok' ? 'The link works' : result.linkStatus === 'dead' ? 'The link is gone' : `Could not tell: ${result.linkDetail}`);
}

async function deleteCurrent() {
  const item = state.current;
  if (!item || !window.confirm(`Delete "${item.title}" with its highlights and note?`)) return;
  await api('DELETE', `/api/items/${item.id}`);
  state.current = null;
  state.currentId = null;
  byId('reader-body').hidden = true;
  byId('reader-empty').hidden = false;
  showToast('Deleted');
  setPane('list');
  await Promise.all([loadLibrary(), loadItems()]);
}

async function loadImagesForCurrent() {
  if (!state.current) return;
  await changeCurrent({ loadImages: true }).catch(reportError);
  await openItem(state.current.id);
}

async function move(step) {
  if (!state.items.length) return;
  const index = state.items.findIndex((item) => item.id === state.currentId);
  let target = index === -1 ? 0 : index + step;
  if (target >= state.items.length) {
    const grew = await loadMore().catch(reportError);
    if (!grew) return;
  }
  target = Math.max(0, Math.min(target, state.items.length - 1));
  if (state.items[target].id !== state.currentId) await openItem(state.items[target].id);
}

function openDialog(id) {
  const dialog = byId(id);
  for (const error of dialog.querySelectorAll('.form-error')) error.hidden = true;
  dialog.showModal();
}

function showFormError(id, error) {
  const box = byId(id);
  box.textContent = error.message || String(error);
  box.hidden = false;
}

function openSaveDialog(url = '') {
  byId('save-url').value = url;
  byId('save-tags').value = state.tag;
  fillCollectionSelect(byId('save-collection'), state.collectionId);
  openDialog('save-dialog');
  if (url) byId('save-submit').focus();
  else byId('save-url').focus();
}

async function afterSave(id) {
  byId('save-dialog').close();
  if (window.location.pathname === '/save') window.history.replaceState(null, '', '/');
  await loadLibrary();
  await selectScope({ view: 'inbox' });
  await openItem(id);
}

async function submitSave(event) {
  event.preventDefault();
  const submit = byId('save-submit');
  submit.disabled = true;
  submit.textContent = 'Fetching...';
  try {
    const item = await api('POST', '/api/items', {
      url: byId('save-url').value.trim(),
      tags: parseTags(byId('save-tags').value),
      collectionId: selectedCollectionId(byId('save-collection')),
    });
    showToast(item.fetchError ? `Saved the link, but the page could not be read: ${item.fetchError}` : `Saved ${item.title}`, Boolean(item.fetchError));
    await afterSave(item.id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && error.data && error.data.id) {
      showToast('That link is already saved');
      await afterSave(error.data.id);
      return;
    }
    showFormError('save-error', error);
  } finally {
    submit.disabled = false;
    submit.textContent = 'Save';
  }
}

function sharedAddress(params) {
  const direct = (params.get('url') || '').trim();
  if (direct) return direct;
  const found = (params.get('text') || '').match(WEB_ADDRESS);
  return found ? found[0] : '';
}

function openSharedLink() {
  if (window.location.pathname !== '/save') return;
  const address = sharedAddress(new URLSearchParams(window.location.search));
  if (address) openSaveDialog(address);
  else window.history.replaceState(null, '', '/');
}

function openCollectionDialog(collection) {
  state.editingCollection = collection;
  byId('collection-dialog-title').textContent = collection ? 'Collection' : 'New collection';
  byId('collection-name').value = collection ? collection.name : '';
  byId('collection-delete').hidden = !collection;
  openDialog('collection-dialog');
  byId('collection-name').focus();
}

async function submitCollection(event) {
  event.preventDefault();
  const name = byId('collection-name').value.trim();
  const collection = state.editingCollection;
  try {
    if (collection) await api('PATCH', `/api/collections/${collection.id}`, { name });
    else await api('POST', '/api/collections', { name });
    byId('collection-dialog').close();
    await loadLibrary();
  } catch (error) {
    showFormError('collection-error', error);
  }
}

async function deleteEditingCollection() {
  const collection = state.editingCollection;
  if (!window.confirm(`Delete the collection ${collection.name}? Its links stay saved, outside any collection.`)) return;
  try {
    await api('DELETE', `/api/collections/${collection.id}`);
    byId('collection-dialog').close();
    await loadLibrary();
    await selectScope({ view: 'inbox' });
  } catch (error) {
    showFormError('collection-error', error);
  }
}

function openScopeSettings() {
  const collection = collectionById(state.collectionId);
  if (collection) openCollectionDialog(collection);
}

function openBookmarklet() {
  const origin = window.location.origin;
  const code = `(()=>{window.open('${origin}/save?url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(document.title),'_blank','noopener,width=560,height=720')})()`;
  byId('bookmarklet-link').href = `javascript:${code}`;
  byId('share-address').textContent = `${origin}/save?url=`;
  openDialog('bookmarklet-dialog');
}

async function importFile(file) {
  if (!file) return;
  try {
    const result = await api('POST', '/api/import', await file.arrayBuffer(), file.type || 'application/octet-stream');
    showToast(`Imported ${result.added}, skipped ${result.skipped} already saved or not web links.`);
    await Promise.all([loadLibrary(), loadItems()]);
  } catch (error) {
    reportError(error);
  } finally {
    byId('import-file').value = '';
  }
}

function showCheckStatus(status) {
  const box = byId('check-progress');
  box.hidden = false;
  if (status.running) box.textContent = `Checking links: ${status.checked} of ${status.total}, ${status.dead} gone so far.`;
  else if (status.error) box.textContent = `The check stopped: ${status.error}`;
  else box.textContent = `Checked ${status.checked} links, ${status.dead} gone.`;
}

async function checkAllLinks() {
  const button = byId('check-all-btn');
  button.disabled = true;
  try {
    let status = await api('POST', '/api/link-check', { view: 'all' });
    showCheckStatus(status);
    while (status.running) {
      await sleep(CHECK_POLL_MS);
      status = await api('GET', '/api/link-check');
      showCheckStatus(status);
    }
    await Promise.all([loadLibrary(), loadItems()]);
  } finally {
    button.disabled = false;
  }
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));
}

function backToList() {
  if (document.body.dataset.pane !== 'list') {
    saveProgress().catch(reportError);
    setPane('list');
    const node = document.querySelector('.item[aria-current="true"]');
    if (node) node.focus();
  }
}

const KEY_ACTIONS = {
  j: () => move(1),
  k: () => move(-1),
  f: toggleFavorite,
  a: () => toggleArchive().catch(reportError),
  h: () => highlightSelection().catch(reportError),
  v: () => { if (state.current) window.open(state.current.url, '_blank', 'noopener,noreferrer'); },
  n: () => openSaveDialog(),
  '/': () => byId('search').focus(),
  '?': () => openDialog('keys-dialog'),
  Escape: backToList,
};

function handleKey(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (document.querySelector('dialog[open]')) return;
  if (isTyping(event.target)) {
    if (event.key === 'Escape') event.target.blur();
    return;
  }
  const action = KEY_ACTIONS[event.key];
  if (!action) return;
  event.preventDefault();
  action();
}

let searchTimer = 0;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = byId('search').value.trim();
    renderSidebar();
    renderListHead();
    setPane('list');
    loadItems().catch(reportError);
  }, SEARCH_DELAY_MS);
}

function wireEvents() {
  for (const button of document.querySelectorAll('.views .nav-btn')) {
    button.addEventListener('click', () => selectScope({ view: button.dataset.view }));
  }
  for (const button of document.querySelectorAll('[data-close]')) {
    button.addEventListener('click', () => button.closest('dialog').close());
  }
  byId('menu-btn').addEventListener('click', () => setPane(document.body.dataset.pane === 'nav' ? 'list' : 'nav'));
  byId('search').addEventListener('input', onSearchInput);
  byId('save-btn').addEventListener('click', () => openSaveDialog());
  byId('save-form').addEventListener('submit', submitSave);
  byId('collection-form').addEventListener('submit', submitCollection);
  byId('collection-delete').addEventListener('click', deleteEditingCollection);
  byId('new-collection-btn').addEventListener('click', () => openCollectionDialog(null));
  byId('scope-settings').addEventListener('click', openScopeSettings);
  byId('import-btn').addEventListener('click', () => byId('import-file').click());
  byId('import-file').addEventListener('change', (event) => importFile(event.target.files[0]));
  byId('bookmarklet-btn').addEventListener('click', openBookmarklet);
  byId('bookmarklet-link').addEventListener('click', (event) => {
    event.preventDefault();
    showToast('Drag this button to your bookmarks bar instead of clicking it.');
  });
  byId('keys-btn').addEventListener('click', () => openDialog('keys-dialog'));
  byId('check-all-btn').addEventListener('click', () => checkAllLinks().catch(reportError));
  byId('more-btn').addEventListener('click', () => loadMore().catch(reportError));
  byId('favorite-btn').addEventListener('click', toggleFavorite);
  byId('archive-btn').addEventListener('click', () => toggleArchive().catch(reportError));
  byId('highlight-btn').addEventListener('mousedown', (event) => event.preventDefault());
  byId('highlight-btn').addEventListener('click', () => highlightSelection().catch(reportError));
  byId('check-btn').addEventListener('click', () => checkCurrentLink().catch(reportError));
  byId('delete-btn').addEventListener('click', () => deleteCurrent().catch(reportError));
  byId('refetch-btn').addEventListener('click', () => refetchCurrent().catch(reportError));
  byId('back-btn').addEventListener('click', backToList);
  byId('load-images-btn').addEventListener('click', loadImagesForCurrent);
  byId('note').addEventListener('input', onNoteInput);
  byId('reader-tags').addEventListener('change', saveReaderTags);
  byId('reader-tags').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') event.target.blur();
  });
  byId('reader-collection').addEventListener('change', saveReaderCollection);
  byId('reader').addEventListener('scroll', onReaderScroll, { passive: true });
  window.addEventListener('scroll', () => { if (phoneScreen.matches) onReaderScroll(); }, { passive: true });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') saveProgress().catch(reportError);
  });
  document.addEventListener('keydown', handleKey);
}

async function start() {
  wireEvents();
  try {
    const session = await api('GET', '/api/session');
    state.csrf = session.csrfToken;
    await loadLibrary();
    await loadItems();
    openSharedLink();
  } catch (error) {
    reportError(error);
  }
}

start();
