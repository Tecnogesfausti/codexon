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
  document.body.appendChild(toolbar);
  requestAnimationFrame(() => dispatchEvent(new Event('resize')));
})();
