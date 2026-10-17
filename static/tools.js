/**
 * Tools settings: switch each of her abilities on or off.
 *
 * The switches live on the server (tool_settings.json) and are ENFORCED there —
 * /api/actions refuses a switched-off tool no matter what the page or the
 * model does. This file only draws them and flips them.
 */
(function () {
    'use strict';

    let state = null;
    let busy = false;
    let changeTimer = null;

    // Flipping several switches in a row should reconnect a live call ONCE,
    // with the final state, not once per click.
    function notifyChanged() {
        clearTimeout(changeTimer);
        changeTimer = setTimeout(() => {
            if (typeof window.AISToolsChanged === 'function') window.AISToolsChanged();
        }, 900);
    }

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    function status(msg, cls) {
        const s = document.getElementById('toolsStatus');
        if (!s) return;
        s.textContent = msg || '';
        s.className = 'tools-status' + (cls ? ' ' + cls : '');
    }

    /** A proper switch: a real checkbox underneath, so it is keyboard-usable. */
    function makeSwitch(checked, label, onChange, opts) {
        opts = opts || {};
        const wrap = el('label', 'tsw' + (opts.partial ? ' partial' : ''));
        const input = el('input');
        input.type = 'checkbox';
        input.checked = checked;
        input.setAttribute('aria-label', label);
        if (opts.partial) input.indeterminate = true;
        input.addEventListener('change', () => onChange(input.checked, input));
        const track = el('span', 'tsw-track');
        track.appendChild(el('span', 'tsw-thumb'));
        wrap.append(input, track);
        return wrap;
    }

    function render() {
        const list = document.getElementById('toolsList');
        if (!list || !state) return;
        list.textContent = '';

        state.groups.forEach((g) => {
            const card = el('div', 'tgroup' + (g.enabled ? '' : g.partial ? ' partial' : ' off'));

            const head = el('div', 'tgroup-head');
            const title = el('div', 'tgroup-title');
            title.append(el('span', 'tgroup-icon', g.icon), el('span', null, g.label));
            const count = g.tools.filter((t) => t.enabled).length;
            const meta = el('span', 'tgroup-meta', count + '/' + g.tools.length);
            head.append(title, meta,
                makeSwitch(g.enabled, g.label, (on) => set({ group: g.id, enabled: on }),
                           { partial: g.partial }));
            card.appendChild(head);

            // Only list the individual tools when the group has more than one.
            if (g.tools.length > 1) {
                const rows = el('div', 'tgroup-tools');
                g.tools.forEach((t) => {
                    const row = el('div', 'trow' + (t.enabled ? '' : ' off'));
                    const name = el('span', 'trow-name', t.label);
                    if (t.confirm) {
                        name.appendChild(el('span', 'trow-tag', 'asks first'));
                    }
                    row.append(name,
                        makeSwitch(t.enabled, t.label,
                                   (on) => set({ tool: t.id, enabled: on })));
                    rows.appendChild(row);
                });
                card.appendChild(rows);
            } else if (g.tools[0] && g.tools[0].confirm) {
                head.querySelector('.tgroup-title')
                    .appendChild(el('span', 'trow-tag', 'asks first'));
            }

            list.appendChild(card);
        });
    }

    async function load() {
        try {
            state = await fetch('/api/tools').then((r) => r.json());
            render();
            status('');
        } catch (e) {
            status('Could not load tool settings — is the server running?', 'err');
        }
    }

    async function set(body) {
        if (busy) return;
        busy = true;
        try {
            const res = await fetch('/api/tools', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            state = await res.json();
            render();
            const off = state.disabled.length;
            status(off ? off + ' abilit' + (off === 1 ? 'y' : 'ies') + ' switched off.'
                       : 'Everything is on.', 'ok');
            // Let a live call pick the change up (switching ON needs a reconnect).
            notifyChanged();
        } catch (e) {
            status('Could not save — ' + e.message, 'err');
            await load();       // re-sync the switches with what the server has
        } finally {
            busy = false;
        }
    }

    async function setAll(on) {
        if (!state || busy) return;
        busy = true;
        try {
            for (const g of state.groups) {
                const res = await fetch('/api/tools', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ group: g.id, enabled: on })
                });
                state = await res.json();
            }
            render();
            status(on ? 'Everything is on.' : 'Everything is off — she can only talk.', 'ok');
            notifyChanged();
        } catch (e) {
            status('Could not save — ' + e.message, 'err');
            await load();
        } finally {
            busy = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const on = document.getElementById('toolsAllOn');
        const off = document.getElementById('toolsAllOff');
        if (on) on.addEventListener('click', () => setAll(true));
        if (off) off.addEventListener('click', () => setAll(false));

        // Refresh whenever the tab is opened, in case another window changed it.
        const tab = document.querySelector('.mode-tab[data-mode="tools"]');
        if (tab) tab.addEventListener('click', load);

        load();
    });

    window.AISTools = { reload: load };
})();
