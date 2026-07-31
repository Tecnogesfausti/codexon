(() => {
  if (document.getElementById('segurai-memory-page')) return;

  const nav = document.querySelector('nav.tabs');
  const grid = document.querySelector('.grid');
  if (!nav || !grid) return;

  const style = document.createElement('style');
  style.textContent = `
    #segurai-memory-page .memory-toolbar {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; margin-bottom: 14px;
    }
    #segurai-memory-state { color: var(--muted, #94a3b8); }
    #segurai-memory-list { display: grid; gap: 10px; }
    .segurai-memory-row {
      padding: 12px; border: 1px solid var(--border, #273449);
      border-radius: 10px; background: rgba(15, 23, 42, .55);
    }
    .segurai-memory-head {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px;
      margin-bottom: 7px;
    }
    .segurai-memory-kind {
      padding: 3px 7px; border-radius: 999px; background: #7c2d12;
      color: #fff; font-size: 12px; font-weight: 700;
    }
    .segurai-memory-topic { font-weight: 700; }
    .segurai-memory-date, .segurai-memory-meta {
      color: var(--muted, #94a3b8); font-size: 12px;
    }
    .segurai-memory-date { margin-left: auto; }
    .segurai-memory-content { margin: 0 0 8px; white-space: pre-wrap; }
    @media (max-width: 640px) {
      #segurai-memory-page .memory-toolbar { align-items: flex-start; }
      .segurai-memory-date { width: 100%; margin-left: 0; }
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
  section.id = 'segurai-memory-page';
  section.className = 'full';
  section.dataset.page = 'memorias';

  const toolbar = document.createElement('div');
  toolbar.className = 'memory-toolbar';
  const heading = document.createElement('div');
  const title = document.createElement('h2');
  title.textContent = 'Memorias recientes';
  const state = document.createElement('div');
  state.id = 'segurai-memory-state';
  state.textContent = 'Preparando memorias…';
  heading.append(title, state);
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'secondary';
  refresh.textContent = 'Actualizar';
  toolbar.append(heading, refresh);
  const list = document.createElement('div');
  list.id = 'segurai-memory-list';
  section.append(toolbar, list);
  grid.appendChild(section);

  function formatDate(value) {
    if (!value) return '-';
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleString('es-ES');
  }

  function memoryRow(memory) {
    const row = document.createElement('article');
    row.className = 'segurai-memory-row';
    const head = document.createElement('div');
    head.className = 'segurai-memory-head';
    const kind = document.createElement('span');
    kind.className = 'segurai-memory-kind';
    kind.textContent = memory.kind || 'memoria';
    const topic = document.createElement('span');
    topic.className = 'segurai-memory-topic';
    topic.textContent = memory.topic || 'Sin tema';
    const date = document.createElement('span');
    date.className = 'segurai-memory-date';
    date.textContent = formatDate(memory.created_at);
    head.append(kind, topic, date);
    const content = document.createElement('p');
    content.className = 'segurai-memory-content';
    content.textContent = memory.content || '';
    const meta = document.createElement('div');
    meta.className = 'segurai-memory-meta';
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
      console.warn('No se pudo actualizar el resumen SegurAI', error);
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
