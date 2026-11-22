/**
 * Large on-screen keyboard for touchscreens. Off unless the page is opened with
 * ?keyboard=1 (Lumalien opens LumaKit that way on its 7-inch screen, and not in
 * its desktop mode). It types into whichever text field has focus. Keys act on
 * pointerdown and never take focus, so the caret stays in the field.
 * The same keyboard as Lumalien's own (robot_console/web/keyboard.js there).
 */
export function setupKeyboard() {
    if (new URLSearchParams(location.search).get('keyboard') !== '1') return;
    let target = null, layer = 'letters', shift = 'off', lastShift = 0, repeat = null;

    // [label, action, width units]
    const LETTERS = [
        'qwertyuiop'.split('').map(c => [c, c]),
        [['', 'spacer', 0.5], ...'asdfghjkl'.split('').map(c => [c, c]), ['', 'spacer', 0.5]],
        [['⇧', 'shift', 1.5], ...'zxcvbnm'.split('').map(c => [c, c]), ['⌫', 'backspace', 1.5]],
        [['123', 'layer:symbols', 1.5], ['◀', 'left'], [',', ','], ['space', ' ', 4], ['.', '.'], ['▶', 'right'], ['⏎', 'enter', 1.5], ['⌄', 'hide']],
    ];
    const SYMBOLS = [
        '1234567890'.split('').map(c => [c, c]),
        '-/:;()$&@"'.split('').map(c => [c, c]),
        [['#+=', 'layer:more', 1.5], ...".,?!'".split('').map(c => [c, c]), ['⌫', 'backspace', 1.5]],
        [['ABC', 'layer:letters', 1.5], ['◀', 'left'], ['space', ' ', 5], ['▶', 'right'], ['⏎', 'enter', 1.5], ['⌄', 'hide']],
    ];
    const MORE = [
        '[]{}#%^*+='.split('').map(c => [c, c]),
        '_\\|~<>€£¥•'.split('').map(c => [c, c]),
        [['123', 'layer:symbols', 1.5], ['tab', 'tab'], ['⌫', 'backspace', 1.5]],
        [['ABC', 'layer:letters', 1.5], ['◀', 'left'], ['space', ' ', 5], ['▶', 'right'], ['⏎', 'enter', 1.5], ['⌄', 'hide']],
    ];
    const LAYERS = { letters: LETTERS, symbols: SYMBOLS, more: MORE };
    const NAMES = { ' ': 'Space', backspace: 'Backspace', enter: 'Enter', hide: 'Hide keyboard', shift: 'Shift',
        left: 'Move left', right: 'Move right', tab: 'Tab' };

    const panel = document.createElement('div');
    panel.className = 'osk';
    panel.hidden = true;
    panel.setAttribute('role', 'group');
    panel.setAttribute('aria-label', 'On-screen keyboard');
    document.body.append(panel);

    function render() {
        panel.replaceChildren();
        for (const row of LAYERS[layer]) {
            const line = document.createElement('div');
            line.className = 'osk-row';
            for (const [label, action, units = 1] of row) {
                const key = document.createElement(action === 'spacer' ? 'span' : 'button');
                key.style.flexGrow = String(units);
                key.className = 'osk-key' + (action === 'spacer' ? ' osk-spacer' : '') + (label.length > 1 || !/^[a-z0-9]$/i.test(label) ? ' osk-fn' : '');
                if (action !== 'spacer') {
                    key.type = 'button';
                    key.tabIndex = -1;
                    key.dataset.action = action;
                    const letter = layer === 'letters' && /^[a-z]$/.test(action);
                    key.textContent = letter && shift !== 'off' ? label.toUpperCase() : label;
                    if (action === 'shift') { key.classList.toggle('active', shift !== 'off'); key.classList.toggle('locked', shift === 'lock'); }
                    key.setAttribute('aria-label', NAMES[action] || key.textContent);
                }
                line.append(key);
            }
            panel.append(line);
        }
    }

    // -- targets ------------------------------------------------------------------
    const TEXT = 'textarea, input:not([type]), input[type="text"], input[type="search"], input[type="email"], input[type="url"], input[type="password"]';
    const editable = el => Boolean(el && el.matches?.(TEXT) && !el.disabled && !el.readOnly);

    function show(el) {
        if (!editable(el)) return;
        target = el;
        const home = el.closest('dialog[open]') || document.body;  // modal dialogs make the rest of the page inert
        if (panel.parentElement !== home) home.append(panel);
        if (panel.hidden) { layer = 'letters'; shift = 'off'; render(); }
        panel.hidden = false;
        document.body.classList.add('osk-open');
        requestAnimationFrame(() => el.scrollIntoView({ block: 'nearest' }));
    }
    function hide() {
        stopRepeat();
        panel.hidden = true;
        document.body.classList.remove('osk-open');
        if (panel.parentElement !== document.body) document.body.append(panel);
        target = null;
    }
    document.addEventListener('focusin', event => show(event.target));
    document.addEventListener('focusout', () => {
        setTimeout(() => { if (!editable(document.activeElement)) hide(); }, 150);
    });
    // Tapping an already-focused field brings a hidden keyboard back.
    document.addEventListener('pointerdown', event => {
        if (editable(event.target) && event.target === document.activeElement && panel.hidden) show(event.target);
    });

    // -- typing ---------------------------------------------------------------------
    function insert(text) {
        const start = target.selectionStart ?? target.value.length, end = target.selectionEnd ?? start;
        target.setRangeText(text, start, end, 'end');
        target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    }
    function backspace() {
        let start = target.selectionStart ?? target.value.length;
        const end = target.selectionEnd ?? start;
        if (start === end) {
            if (!start) return;
            start -= /[\uDC00-\uDFFF]/.test(target.value[start - 1]) && start > 1 ? 2 : 1;
        }
        target.setRangeText('', start, end, 'end');
        target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
    }
    function move(delta) {
        const position = Math.max(0, Math.min(target.value.length, (delta < 0 ? target.selectionStart : target.selectionEnd) + delta));
        target.setSelectionRange(position, position);
    }
    // Enter goes to the field first (the composer sends on Enter); otherwise a textarea gets a new line
    // and a field in a form submits it. After sending, the keyboard gets out of the way.
    function enter() {
        const event = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true });
        if (!target.dispatchEvent(event)) { hide(); return; }
        if (target.tagName === 'TEXTAREA') insert('\n');
        else if (target.form) { target.form.requestSubmit(); hide(); }
    }
    function press(action) {
        if (!target || !document.contains(target)) { hide(); return; }
        if (action.startsWith('layer:')) { layer = action.slice(6); render(); return; }
        switch (action) {
            case 'shift': {
                const now = performance.now();
                shift = shift === 'off' ? (now - lastShift < 350 ? 'lock' : 'once') : shift === 'once' && now - lastShift < 350 ? 'lock' : 'off';
                lastShift = now;
                render();
                return;
            }
            case 'backspace': backspace(); break;
            case 'enter': enter(); break;
            case 'hide': hide(); return;
            case 'left': move(-1); break;
            case 'right': move(1); break;
            case 'tab': insert('\t'); break;
            default: {
                insert(layer === 'letters' && shift !== 'off' ? action.toUpperCase() : action);
                if (shift === 'once') { shift = 'off'; render(); }
                if (layer !== 'letters' && action === ' ') { layer = 'letters'; render(); }
            }
        }
    }
    function stopRepeat() { if (repeat) { clearTimeout(repeat.delay); clearInterval(repeat.timer); repeat = null; } }
    panel.addEventListener('pointerdown', event => {
        event.preventDefault();  // keep focus (and the caret) in the field
        const key = event.target.closest('button.osk-key');
        if (!key) return;
        key.classList.add('pressed');
        setTimeout(() => key.classList.remove('pressed'), 120);
        const action = key.dataset.action;
        press(action);
        if (action === 'backspace' || action === 'left' || action === 'right') {
            stopRepeat();
            repeat = { delay: setTimeout(() => { repeat.timer = setInterval(() => press(action), 70); }, 450) };
        }
    });
    for (const type of ['pointerup', 'pointercancel', 'pointerleave']) panel.addEventListener(type, stopRepeat);
    panel.addEventListener('mousedown', event => event.preventDefault());
    render();
}
