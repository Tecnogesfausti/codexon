(() => {
  if (document.getElementById('codexon-memory-page')) return;

  const nav = document.querySelector('nav.tabs');
  const grid = document.querySelector('.grid');
  if (!nav || !grid) return;

  const style = document.createElement('style');
  style.textContent = `
    #codexon-memory-page .memory-toolbar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin-bottom: 14px;
    }
    #codexon-memory-state { color: var(--muted, #94a3b8); }
    #codexon-memory-list { display: grid; gap: 10px; }
    .codexon-memory-row {
      padding: 12px; border: 1px solid var(--border, #273449);
      border-radius: 10px; background: rgba(15, 23, 42, .55);
    }
    .codexon-memory-head {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px;
      margin-bottom: 7px;
    }
    .codexon-memory-kind {
      padding: 3px 7px; border-radius: 999px; background: #7c2d12;
      color: #fff; font-size: 12px; font-weight: 700;
    }
    .codexon-memory-topic { font-weight: 700; }
    .codexon-memory-date, .codexon-memory-meta {
      color: var(--muted, #94a3b8); font-size: 12px;
    }
    .codexon-memory-date { margin-left: auto; }
    .codexon-memory-content { margin: 0 0 8px; white-space: pre-wrap; }
    @media (max-width: 640px) {
      #codexon-memory-page .memory-toolbar { align-items: flex-start; }
      .codexon-memory-date { width: 100%; margin-left: 0; }
    }
  `;
  document.head.appendChild(style);

  const tab = document.createElement('button');
  tab.type = 'button';
  tab.className = 'tab';
  tab.dataset.tab = 'memorias';
  tab.textContent = 'Memorias';
  nav.appendChild(tab);

  const section = document.createElement('section');
  section.id = 'codexon-memory-page';
  section.className = 'full';
  section.dataset.page = 'memorias';

  const toolbar = document.createElement('div');
  toolbar.className = 'memory-toolbar';
  const heading = document.createElement('div');
  const title = document.createElement('h2');
  title.textContent = 'Memorias recientes';
  const state = document.createElement('div');
  state.id = 'codexon-memory-state';
  state.textContent = 'Preparando memorias…';
  heading.append(title, state);
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'secondary';
  refresh.textContent = 'Actualizar';
  toolbar.append(heading, refresh);
  const list = document.createElement('div');
  list.id = 'codexon-memory-list';
  section.append(toolbar, list);
  grid.appendChild(section);

  function formatDate(value) {
    if (!value) return '-';
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString('es-ES');
  }

  function memoryRow(memory) {
    const row = document.createElement('article');
    row.className = 'codexon-memory-row';
    const head = document.createElement('div');
    head.className = 'codexon-memory-head';
    const kind = document.createElement('span');
    kind.className = 'codexon-memory-kind';
    kind.textContent = memory.kind || 'memoria';
    const topic = document.createElement('span');
    topic.className = 'codexon-memory-topic';
    topic.textContent = memory.topic || 'Sin tema';
    const date = document.createElement('span');
    date.className = 'codexon-memory-date';
    date.textContent = formatDate(memory.created_at);
    head.append(kind, topic, date);
    const content = document.createElement('p');
    content.className = 'codexon-memory-content';
    content.textContent = memory.content || '';
    const meta = document.createElement('div');
    meta.className = 'codexon-memory-meta';
    const confidence = Number(memory.confidence);
    const confidenceText = Number.isFinite(confidence) ? `${Math.round(confidence * 100)}%` : '-';
    meta.textContent = `Confianza ${confidenceText} · origen ${memory.source || '-'}`;
    row.append(head, content, meta);
    return row;
  }

  async function request(path) {
    const response = await fetch(path, {headers: {'Content-Type': 'application/json'}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  async function refreshSummary() {
    try {
      const status = await request('api/status');
      const memories = document.getElementById('memories');
      const observations = document.getElementById('observations');
      const calls = document.getElementById('calls');
      const cost = document.getElementById('cost');
      if (memories) memories.textContent = status.memories ?? '-';
      if (observations) observations.textContent = status.observations ?? '-';
      if (calls) calls.textContent = status.usage?.calls ?? '-';
      if (cost) cost.textContent = '$' + Number(status.usage?.cost || 0).toFixed(5);
    } catch (error) {
      console.warn('No se pudo actualizar el resumen Codexon', error);
    }
  }

  async function loadMemories() {
    refresh.disabled = true;
    state.textContent = 'Cargando memorias…';
    try {
      const memories = await request('api/memories?limit=100');
      list.replaceChildren(...memories.map(memoryRow));
      if (!memories.length) {
        const empty = document.createElement('p');
        empty.textContent = 'Todavía no hay memorias guardadas.';
        list.appendChild(empty);
      }
      state.textContent = `${memories.length} memorias recientes`;
    } catch (error) {
      list.replaceChildren();
      state.textContent = `No se pudieron cargar las memorias: ${error.message}`;
    } finally {
      refresh.disabled = false;
    }
  }

  tab.addEventListener('click', () => {
    if (typeof window.setPage === 'function') window.setPage('memorias');
    loadMemories();
  });
  refresh.addEventListener('click', loadMemories);

  const originalLoadAll = window.loadAll;
  if (typeof originalLoadAll === 'function') {
    window.loadAll = async (...args) => {
      try {
        await originalLoadAll(...args);
      } catch (error) {
        if (typeof window.showNotice === 'function') {
          window.showNotice(`Carga parcial: ${error.message}`, 'error');
        }
      } finally {
        await refreshSummary();
      }
    };
  }

  refreshSummary();
})();
