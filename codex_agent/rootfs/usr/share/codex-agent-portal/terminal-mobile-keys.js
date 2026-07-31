(() => {
  const mobile = matchMedia('(pointer: coarse), (max-width: 720px)');
  if (!mobile.matches || document.getElementById('codex-mobile-keys')) return;

  const style = document.createElement('style');
  style.textContent = `
    #terminal-container { height: calc(100% - 50px) !important; }
    #codex-mobile-keys {
      position: fixed; inset: auto 0 0; z-index: 10000; height: 50px;
      display: flex; align-items: center; gap: 6px; padding: 6px 8px;
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
    ['Esc', 'Escape', 'Escape', 27],
    ['Tab', 'Tab', 'Tab', 9],
    ['Ctrl+C', 'c', 'KeyC', 67, true],
    ['Ctrl+X', 'x', 'KeyX', 88, true],
    ['Ctrl+Z', 'z', 'KeyZ', 90, true],
    ['Ctrl+L', 'l', 'KeyL', 76, true],
    ['Ctrl+D', 'd', 'KeyD', 68, true],
    ['Ctrl+R', 'r', 'KeyR', 82, true],
    ['←', 'ArrowLeft', 'ArrowLeft', 37],
    ['↑', 'ArrowUp', 'ArrowUp', 38],
    ['↓', 'ArrowDown', 'ArrowDown', 40],
    ['→', 'ArrowRight', 'ArrowRight', 39],
  ];

  function textarea() {
    return document.querySelector('.xterm-helper-textarea');
  }

  function sendKey(key, code, keyCode, ctrlKey = false) {
    const target = textarea();
    if (!target) return;
    target.focus({preventScroll: true});
    const init = {key, code, keyCode, which: keyCode, ctrlKey, bubbles: true, cancelable: true};
    target.dispatchEvent(new KeyboardEvent('keydown', init));
    target.dispatchEvent(new KeyboardEvent('keyup', init));
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
  ctrl.addEventListener('pointerdown', event => {
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
    sendKey(event.key, event.code, event.keyCode || event.which, true);
  }, true);

  keys.forEach(([label, key, code, keyCode, ctrlKey]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.setAttribute('aria-label', label);
    button.addEventListener('pointerdown', event => {
      event.preventDefault();
      sendKey(key, code, keyCode, Boolean(ctrlKey));
    });
    toolbar.appendChild(button);
  });
  document.body.appendChild(toolbar);
  requestAnimationFrame(() => dispatchEvent(new Event('resize')));
})();
