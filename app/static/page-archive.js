/* StreamingCommunity Web Panel — page-archive.js */
//
// The archive: a source's whole catalogue under that source's own filters —
// kind, genre, country, year, rating. The filters are not written here. The
// server sends a definition (GET /api/archive/filters) and this page draws
// whatever it holds, so a filter the source adds shows up without a release,
// and what is offered is what the server will accept.
//
// The cards are the search page's, and open the same title page: results go
// into the shared _searchResults, which openTitle() reads.

// The filters on screen, as { key: [values] }. null until the definition is
// in and the defaults are known: until then the address is the only record of
// what was asked, and read() hands it back unchanged.
let _arcDef = null;
let _arcFilters = null;
let _arcRaw = {};            // the address as it arrived, for read() before the definition
let _arcExplicit = false;    // see the note on the address, below

let _arcPage = 1;
let _arcHasMore = false;
let _arcLoading = false;
let _arcEnterToken = 0;
let _arcLoadToken = 0;
let _arcAbort = null;
let _arcDebounce = null;

const _ARC_SOURCES = [
  ['streamingcommunity', 'ti-device-tv', 'StreamingCommunity'],
  ['animeunity', 'ti-sword', 'AnimeUnity'],
];

// ── Entering the page ─────────────────────────────────────────────────────────

// From the "Archivio" button on the search page, on the source it was on.
function archiveOpen() {
  navigate('archive', [], {src: currentSource === 'animeunity' ? 'animeunity' : null});
}

function _arcSwitchSource(source) {
  if (source === currentSource) return;
  navigate('archive', [], {src: source === 'animeunity' ? 'animeunity' : null});
}

async function archiveEnter() {
  if (!(can('REQUEST') || can('DOWNLOAD'))) { showPage(defaultPage()); return; }
  const token = ++_arcEnterToken;
  _arcRenderHead();

  const results = document.getElementById('archive-results');
  results.innerHTML = '';
  document.getElementById('archive-filters').innerHTML = '';
  document.getElementById('archive-count').textContent = '';
  _arcSetMoreVisible(false);
  if (_arcAbort) _arcAbort.abort();

  try {
    if (currentSource !== 'animeunity') {
      await ensureDomain();
      if (!currentDomain) { openSettings(); return; }
    }
    const def = await api.get('/api/archive/filters', {source: currentSource});
    if (token !== _arcEnterToken) return;   // left, or switched source, meanwhile
    _arcDef = def;
    _arcFilters = _arcExplicit ? _arcFromAddress(def, _arcRaw) : _arcFromDefaults(def);
    _arcRenderFilters();
    syncHash();
    archiveLoad(true);
  } catch (e) {
    if (token !== _arcEnterToken) return;
    results.innerHTML =
      `<div class="col-12"><div class="alert alert-danger">${escapeHtml(errText(e))}</div></div>`;
  }
}

function _arcFromDefaults(def) {
  const filters = {};
  for (const flt of def.filters) {
    const value = (def.defaults || {})[flt.key];
    filters[flt.key] = value ? [String(value)] : [];
  }
  return filters;
}

// 'q' is text and may hold a comma; everything else is a list of option values.
function _arcFromAddress(def, raw) {
  const filters = {};
  for (const flt of def.filters) {
    const text = raw[flt.key];
    filters[flt.key] = !text ? [] : (flt.key === 'q' ? [text] : text.split(',').filter(Boolean));
  }
  return filters;
}

// ── The filters ───────────────────────────────────────────────────────────────

function _arcRenderHead() {
  const name = currentSource === 'animeunity' ? 'AnimeUnity' : 'StreamingCommunity';
  document.getElementById('archive-sub').textContent =
    `Il catalogo di ${name}, con i filtri della sorgente.`;
  document.getElementById('archive-sources').innerHTML = _ARC_SOURCES.map(([id, icon, label]) =>
    `<button class="source-btn${id === currentSource ? ' active' : ''}" ` +
    `data-action="archive:source" data-source="${id}"><i class="ti ${icon}"></i>${label}</button>`
  ).join('');
}

function _arcControl(flt) {
  const key = escapeHtml(flt.key);
  const values = _arcFilters[flt.key] || [];

  if (flt.kind === 'text') {
    return `<input type="search" class="form-control form-control-sm" placeholder="Cerca nel catalogo" ` +
           `data-input="archive:text" data-key="${key}" value="${escapeHtml(values[0] || '')}">`;
  }
  if (flt.kind === 'toggle') {
    const on = values[0] === '1';
    return `<button class="source-btn${on ? ' active' : ''}" data-action="archive:toggle" ` +
           `data-key="${key}" data-on="${on ? '0' : '1'}"><i class="ti ti-microphone"></i>` +
           `${on ? 'Sì' : 'Tutti'}</button>`;
  }
  if (flt.kind === 'multi') {
    const picked = new Set(values);
    const boxes = flt.options.map(o =>
      `<label class="arc-check"><input type="checkbox" data-change="archive:multi" ` +
      `data-key="${key}" value="${escapeHtml(o.value)}"${picked.has(o.value) ? ' checked' : ''}>` +
      `<span>${escapeHtml(o.name)}</span></label>`).join('');
    const count = picked.size ? `<span class="badge bg-blue-lt ms-2">${picked.size}</span>` : '';
    return `<details class="arc-multi"><summary class="form-select form-select-sm">` +
           `${picked.size ? 'Selezionati' : 'Tutti'}${count}</summary>` +
           `<div class="arc-multi-menu">${boxes}</div></details>`;
  }
  // select. The empty entry is "no filter" — for an ordering, the source's own.
  const blank = (flt.key === 'sort' || flt.key === 'order') ? 'Predefinito' : 'Tutti';
  const opts = flt.options.map(o =>
    `<option value="${escapeHtml(o.value)}"${values[0] === o.value ? ' selected' : ''}>` +
    `${escapeHtml(o.name)}</option>`).join('');
  return `<select class="form-select form-select-sm" data-change="archive:select" data-key="${key}">` +
         `<option value="">${blank}</option>${opts}</select>`;
}

function _arcRenderFilters() {
  const host = document.getElementById('archive-filters');
  host.innerHTML = _arcDef.filters.map(flt =>
    `<div class="arc-filter${flt.key === 'q' ? ' arc-filter-wide' : ''}">` +
    `<label class="arc-label">${escapeHtml(flt.label)}</label>${_arcControl(flt)}</div>`).join('') +
    '<div class="arc-filter arc-filter-actions">' +
    '<button class="source-btn" data-action="archive:reset" title="Torna ai filtri iniziali">' +
    '<i class="ti ti-restore"></i>Ripristina</button></div>';
}

// A filter changed. The address gets `x`, which is what tells a reload that
// these are the person's choices and not the defaults: hashFor() leaves an
// empty value out of the address, so "cleared the year" and "never touched
// the year" would otherwise be the same link, and Back would put the year
// back on.
function _arcChanged({redraw = false, instant = false} = {}) {
  _arcExplicit = true;
  if (redraw) _arcRenderFilters();
  syncHash();
  clearTimeout(_arcDebounce);
  _arcDebounce = setTimeout(() => archiveLoad(true), instant ? 0 : 300);
}

function _arcSetValues(key, values) {
  _arcFilters[key] = values;
}

// Menus close when you click away; a <details> does not do that on its own.
document.addEventListener('click', e => {
  document.querySelectorAll('.arc-multi[open]').forEach(d => {
    if (!d.contains(e.target)) d.removeAttribute('open');
  });
});

// ── The results ───────────────────────────────────────────────────────────────

function _arcSetMoreVisible(on) {
  const wrap = document.getElementById('archive-more');
  if (wrap) wrap.style.display = on ? '' : 'none';
}

function _arcQuery(page) {
  const qs = new URLSearchParams({source: currentSource, page: String(page)});
  for (const [key, values] of Object.entries(_arcFilters || {})) {
    values.forEach(v => { if (v !== '') qs.append(key, v); });
  }
  return `/api/archive?${qs}`;
}

async function archiveLoad(reset) {
  if (!_arcFilters) return;
  if (!reset && (_arcLoading || !_arcHasMore)) return;

  const token = ++_arcLoadToken;
  const container = document.getElementById('archive-results');
  const moreBtn = document.getElementById('archive-more-btn');

  if (reset) {
    // Only a fresh query cancels what is in flight; a load-more must not abort
    // itself.
    if (_arcAbort) _arcAbort.abort();
    _arcAbort = new AbortController();
    _arcPage = 1;
    _arcHasMore = false;
    _searchResults = [];
    _requestStatus = {};
    _arcSetMoreVisible(false);
    container.innerHTML = '';
    for (let i = 0; i < 12; i++) {
      const col = document.createElement('div');
      col.className = 'col-6 col-sm-4 col-md-3 col-lg-2';
      col.innerHTML = '<div class="skeleton skeleton-card"></div>';
      container.appendChild(col);
    }
  } else {
    _arcPage += 1;
    if (moreBtn) moreBtn.disabled = true;
  }
  _arcLoading = true;

  try {
    const body = await api.get(_arcQuery(_arcPage), undefined,
                               {signal: _arcAbort && _arcAbort.signal});
    if (token !== _arcLoadToken) return;
    if (reset) container.innerHTML = '';

    renderResultCards(body.items, container, _searchResults.length);
    _searchResults = _searchResults.concat(body.items);
    _arcHasMore = !!body.has_more;

    document.getElementById('archive-count').textContent = body.total != null
      ? `${Number(body.total).toLocaleString('it-IT')} titoli`
      : `${_searchResults.length} titoli`;
    if (!_searchResults.length) {
      container.innerHTML = '<div class="col-12"><p class="text-muted">Nessun risultato.</p></div>';
    }
    _arcSetMoreVisible(_arcHasMore);
    loadRequestStatuses(body.items.filter(r => r.type !== 'movie').map(r => String(r.id)));
  } catch (e) {
    if (e.name === 'AbortError' || token !== _arcLoadToken) return;
    if (reset) {
      container.innerHTML =
        `<div class="col-12"><div class="alert alert-danger">${escapeHtml(errText(e))}</div></div>`;
    } else {
      _arcPage -= 1;
      showToast(errText(e), 'danger');
      _arcSetMoreVisible(true);
    }
  } finally {
    if (token === _arcLoadToken) _arcLoading = false;
    if (moreBtn) moreBtn.disabled = false;
  }
}

// ── Delegated handlers ────────────────────────────────────────────────────────

registerActions({
  'archive:open':   () => archiveOpen(),
  'archive:source': d => _arcSwitchSource(d.source),
  'archive:more':   () => archiveLoad(false),
  'archive:reset':  () => {
    _arcExplicit = false;
    _arcFilters = _arcFromDefaults(_arcDef);
    _arcRenderFilters();
    syncHash();
    archiveLoad(true);
  },
  'archive:text': (d, el) => {
    _arcSetValues(d.key, el.value.trim() ? [el.value.trim()] : []);
    _arcChanged();
  },
  'archive:select': (d, el) => {
    _arcSetValues(d.key, el.value ? [el.value] : []);
    _arcChanged({instant: true});
  },
  'archive:toggle': d => {
    _arcSetValues(d.key, d.on === '1' ? ['1'] : []);
    _arcChanged({redraw: true, instant: true});
  },
  // Not redrawn: it would close the menu the person is still ticking boxes in.
  'archive:multi': (d, el) => {
    const picked = new Set(_arcFilters[d.key] || []);
    if (el.checked) picked.add(el.value); else picked.delete(el.value);
    _arcSetValues(d.key, [...picked]);
    const badge = el.closest('.arc-multi').querySelector('summary');
    badge.innerHTML = `${picked.size ? 'Selezionati' : 'Tutti'}` +
      (picked.size ? `<span class="badge bg-blue-lt ms-2">${picked.size}</span>` : '');
    _arcChanged();
  },
});


// ── The address ───────────────────────────────────────────────────────────────
//
// #/archive?src=animeunity            the defaults of that source
// #/archive?src=animeunity&x=1&year=2024&genre=51,9
//                                      exactly what the person chose
//
// `x` is the line between the two: with it, a key that is absent means the
// filter is off; without it, the source's defaults apply. Without that line a
// filter the person cleared could never stay cleared across a reload.

registerPageHash('archive', {
  read: () => {
    if (!_arcFilters) return {params: {..._arcRaw}};
    const params = {src: currentSource === 'animeunity' ? 'animeunity' : null};
    if (_arcExplicit) {
      params.x = '1';
      for (const [key, values] of Object.entries(_arcFilters)) {
        if (values.length) params[key] = key === 'q' ? values[0] : values.join(',');
      }
    }
    return {params};
  },
  apply: params => {
    // Not rendered here (showPage() calls archiveEnter() for that), and the
    // source is global because the cards and the title page read it.
    currentSource = params.src === 'animeunity' ? 'animeunity' : 'streamingcommunity';
    _arcRaw = {...params};
    _arcExplicit = params.x === '1';
    _arcFilters = null;
    _arcDef = null;
  },
});
