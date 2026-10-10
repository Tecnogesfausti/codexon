(() => {
  if (document.getElementById('codex-mobile-keys')) return;

  const style = document.createElement('style');
  style.textContent = `
    #terminal-container { height: calc(100% - 50px - env(safe-area-inset-bottom, 0px)) !important; }
    #codex-mobile-keys {
      position: fixed; inset: auto 0 0; z-index: 10000;
      min-height: calc(50px + env(safe-area-inset-bottom, 0px));
      display: flex; align-items: flex-start; gap: 6px;
      padding: 6px 8px calc(6px + env(safe-area-inset-bottom, 0px));
      overflow-x: auto; overscroll-behavior-x: contain; scrollbar-width: none;
      background: #111827; border-top: 1px solid #273449;
    }
    #codex-mobile-keys::-webkit-scrollbar { display: none; }
    #codex-mobile-keys button {
      flex: 0 0 auto; min-width: 42px; height: 36px; padding: 0 10px;
      border: 1px solid #475569; border-radius: 7px;
      background: #1e293b; color: #e5e7eb;
      font: 600 13px/1 system-ui, sans-serif; touch-action: manipulation;
    }
    #codex-mobile-keys button:active { background: #f97316; color: #111827; }
    #codex-mobile-keys button.armed { border-color: #f97316; background: #7c2d12; color: #fff; }
  `;
  document.head.appendChild(style);

  const keys = [
    ['Esc', '\x1b'],
    ['Tab', '\x09'],
    ['Ctrl+C', '\x03'],
    ['Ctrl+X', '\x18'],
    ['Ctrl+Z', '\x1a'],
    ['Ctrl+L', '\x0c'],
    ['Ctrl+D', '\x04'],
    ['Ctrl+R', '\x12'],
    ['←', '\x1b[D'],
    ['↑', '\x1b[A'],
    ['↓', '\x1b[B'],
    ['→', '\x1b[C'],
  ];

  function textarea() {
    return document.querySelector('.xterm-helper-textarea');
  }

  function sendData(data) {
    const target = textarea();
    if (!target) return;
    target.focus({preventScroll: true});
    target.dispatchEvent(new InputEvent('input', {
      data,
      inputType: 'insertText',
      bubbles: true,
      cancelable: true,
    }));
  }

  const toolbar = document.createElement('div');
  toolbar.id = 'codex-mobile-keys';
  toolbar.setAttribute('role', 'toolbar');
  toolbar.setAttribute('aria-label', 'Teclas de terminal');

  // Read xterm's buffer rather than the browser selection (canvas has no text DOM).
  function selectedText() {
    return window.term?.getSelection?.() || '';
  }

  const status = document.createElement('span');
  status.setAttribute('role', 'status');
  status.style.cssText = 'color:#cbd5e1;font:13px system-ui;white-space:nowrap;padding:10px 4px';

  function textDialog(text, pasteMode, message) {
    const dialog = document.createElement('dialog');
    dialog.style.cssText = 'width:min(90vw,700px);max-height:85vh;box-sizing:border-box;background:#111827;color:#e5e7eb;border:1px solid #475569;border-radius:12px;padding:16px';
    const title = document.createElement('h3');
    title.textContent = pasteMode ? 'Pegar en la terminal' : 'Texto de la terminal';
    const help = document.createElement('p');
    help.textContent = message;
    const field = document.createElement('textarea');
    field.value = text;
    field.readOnly = !pasteMode;
    field.setAttribute('aria-label', pasteMode ? 'Texto para pegar' : 'Texto para copiar');
    field.style.cssText = 'display:block;width:100%;height:40vh;box-sizing:border-box;font:14px monospace;background:#0b1020;color:#e5e7eb;border:1px solid #475569;padding:8px;user-select:text';
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap';
    function action(label, callback) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.style.cssText = 'padding:12px;border:1px solid #475569;border-radius:7px;background:#1e293b;color:#e5e7eb';
      button.addEventListener('click', callback);
      actions.appendChild(button);
    }
    action(pasteMode ? 'Pegar texto' : 'Copiar texto', async () => {
      if (pasteMode) {
        if (!window.term?.paste) {
          help.textContent = 'La terminal todavía no está conectada.';
          return;
        }
        window.term.paste(field.value);
        dialog.close();
        window.term.focus();
      } else {
        field.focus();
        field.select();
        try {
          if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(field.value);
          else if (!document.execCommand('copy')) throw new Error('clipboard unavailable');
          help.textContent = 'Texto copiado.';
        } catch (_) {
          help.textContent = 'Selecciona el texto y usa Copiar del navegador o del móvil.';
          field.focus();
          field.select();
        }
      }
    });
    action('Cerrar', () => dialog.close());
    dialog.append(title, help, field, actions);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.appendChild(dialog);
    dialog.showModal();
    field.focus();
    if (!pasteMode) field.select();
  }

  function bufferText() {
    const buffer = window.term?.buffer?.active;
    if (!buffer) return '';
    const lines = [];
    for (let i = 0; i < buffer.length; i++) {
      const line = buffer.getLine(i);
      if (!line) continue;
      const text = line.translateToString(true);
      if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
      else lines.push(text);
    }
    return lines.join('\n').replace(/\n+$/, '');
  }

  async function copySelection() {
    const text = selectedText();
    if (!text) {
      status.textContent = 'Selecciona texto o pulsa «Ver texto».';
      return;
    }
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
      status.textContent = 'Texto copiado.';
    } catch (_) {
      textDialog(text, false, 'Puedes seleccionar y copiar este texto con el menú del navegador.');
    }
  }

  async function pasteClipboard() {
    let text = '';
    try { text = await navigator.clipboard.readText(); } catch (_) {}
    textDialog(text, true, 'Revisa el texto o pégalo aquí con el menú del móvil. Pulsa «Pegar texto» para enviarlo a la terminal.');
  }

  for (const [label, callback] of [
    ['Copiar', copySelection],
    ['Pegar', pasteClipboard],
    ['Ver texto', () => textDialog(bufferText(), false, 'Texto disponible en el historial de la terminal. Selecciona el fragmento que quieras copiar.')],
  ]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.addEventListener('pointerdown', event => event.preventDefault());
    button.addEventListener('click', callback);
    toolbar.appendChild(button);
  }

  document.addEventListener('keydown', event => {
    if (event.target !== textarea() || !event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    if (key !== 'c' && key !== 'v') return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (key === 'c') copySelection();
    else pasteClipboard();
  }, true);

  let ctrlArmed = false;
  const ctrl = document.createElement('button');
  ctrl.type = 'button';
  ctrl.textContent = 'Ctrl';
  ctrl.setAttribute('aria-label', 'Control para la siguiente tecla');
  ctrl.setAttribute('aria-pressed', 'false');
  ctrl.addEventListener('pointerdown', event => event.preventDefault());
  ctrl.addEventListener('click', event => {
    event.preventDefault();
    ctrlArmed = !ctrlArmed;
    ctrl.classList.toggle('armed', ctrlArmed);
    ctrl.setAttribute('aria-pressed', String(ctrlArmed));
    textarea()?.focus({preventScroll: true});
  });
  toolbar.appendChild(ctrl);

  document.addEventListener('keydown', event => {
    if (!ctrlArmed || event.ctrlKey || event.altKey || event.metaKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    ctrlArmed = false;
    ctrl.classList.remove('armed');
    ctrl.setAttribute('aria-pressed', 'false');
    if (event.key.length === 1) {
      sendData(String.fromCharCode(event.key.toUpperCase().charCodeAt(0) & 31));
    }
  }, true);

  keys.forEach(([label, data]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('pointerdown', event => event.preventDefault());
    button.addEventListener('click', event => {
      event.preventDefault();
      sendData(data);
    });
    toolbar.appendChild(button);
  });
  toolbar.appendChild(status);
  document.body.appendChild(toolbar);
  requestAnimationFrame(() => dispatchEvent(new Event('resize')));
})();
