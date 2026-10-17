/**
 * Unified history store + sidebar.
 *
 * Records BOTH kinds of work in one place:
 *   - text mode : one analysis (input text, detected type, action taken)
 *   - voice mode: one Gemini Live conversation (transcript + every emotion reading)
 *
 * Persisted in the app's database (brain.db, via /api/history), not the
 * browser: the desktop app's browser profile used to be thrown away on exit,
 * taking the history with it. Anything still in localStorage from before is
 * uploaded once and then removed from the browser.
 */
(function () {
    'use strict';

    const KEY = 'ais_history_v2';
    const LEGACY_KEY = 'ais_history';

    const FACES = {
        neutral: '😐', happy: '😊', excited: '🤩', calm: '😌',
        sad: '😔', frustrated: '😤', angry: '😠',
        anxious: '😰', confused: '😕', tired: '😴'
    };

    /* ------------------------- storage ------------------------- */

    // In-memory copy of the database rows; every change is written through.
    let cache = [];
    const timers = {};

    function read() {
        return cache;
    }

    function post(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
    }

    /** Coalesce rapid updates (a voice call patches its record every turn). */
    function saveSoon(entry, delay) {
        clearTimeout(timers[entry.id]);
        timers[entry.id] = setTimeout(() => {
            delete timers[entry.id];
            post('/api/history', entry).catch(() => { /* retried on the next change */ });
        }, delay == null ? 400 : delay);
    }

    /** Push anything still waiting — used when the page is going away. */
    function flush() {
        Object.keys(timers).forEach((id) => {
            clearTimeout(timers[id]);
            delete timers[id];
            const entry = cache.find((e) => e.id === id);
            if (!entry) return;
            const blob = new Blob([JSON.stringify(entry)], { type: 'application/json' });
            if (!(navigator.sendBeacon && navigator.sendBeacon('/api/history', blob))) {
                post('/api/history', entry).catch(() => {});
            }
        });
    }

    function localOnly(key) {
        try {
            const raw = localStorage.getItem(key);
            const list = raw ? JSON.parse(raw) : [];
            return Array.isArray(list) ? list : [];
        } catch (_) {
            return [];
        }
    }

    /** One-time upload of what the browser kept before the database. */
    async function migrateFromBrowser() {
        const entries = localOnly(KEY);
        localOnly(LEGACY_KEY).forEach((o) => {
            entries.push({
                id: 'legacy-' + Math.random().toString(36).slice(2),
                mode: 'text',
                at: Date.now(),
                text: o.text || '',
                detectedType: o.type || 'unknown',
                action: o.action || ''
            });
        });
        if (!entries.length) return false;
        const r = await post('/api/history/import', { entries: entries });
        if (!r.ok) return false;
        try {
            localStorage.removeItem(KEY);
            localStorage.removeItem(LEGACY_KEY);
        } catch (_) { /* ignore */ }
        return true;
    }

    async function load() {
        try {
            const migrated = await migrateFromBrowser();
            const d = await fetch('/api/history').then((r) => r.json());
            const server = (d && d.entries) || [];
            // Keep anything created while the request was in flight.
            const known = new Set(server.map((e) => e.id));
            const early = cache.filter((e) => !known.has(e.id));
            cache = early.concat(server).sort((a, b) => b.at - a.at);
            if (migrated) console.info('History moved from the browser into the database.');
        } catch (_) {
            /* server unreachable — keep what this page has */
        }
        render();
    }

    function newId() {
        return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
    }

    function add(entry) {
        const e = Object.assign({ id: newId(), at: Date.now() }, entry);
        cache.unshift(e);
        // A voice call is only written once something is said (update() saves
        // it then). Otherwise closing the app mid-call leaves an empty row.
        if (!(e.mode === 'voice' && !(e.turns || []).length)) saveSoon(e, 0);
        render();
        return e.id;
    }

    function update(id, patch) {
        const i = cache.findIndex((e) => e.id === id);
        if (i === -1) return;
        cache[i] = Object.assign({}, cache[i], patch);
        saveSoon(cache[i]);
        render();
    }

    function remove(id) {
        clearTimeout(timers[id]);
        delete timers[id];
        cache = cache.filter((e) => e.id !== id);
        fetch('/api/history/' + encodeURIComponent(id), { method: 'DELETE' }).catch(() => {});
        render();
    }

    function clear() {
        Object.keys(timers).forEach((id) => clearTimeout(timers[id]));
        cache = [];
        fetch('/api/history', { method: 'DELETE' }).catch(() => {});
        render();
    }

    /* ------------------------- helpers ------------------------- */

    /** Most frequent emotion in a session; ties broken by whichever came last. */
    function dominantEmotion(emotions) {
        if (!emotions || !emotions.length) return null;
        const counts = {};
        emotions.forEach((e) => {
            counts[e.emotion] = (counts[e.emotion] || 0) + 1;
        });
        let best = null;
        let bestN = -1;
        emotions.forEach((e) => {
            const n = counts[e.emotion];
            if (n >= bestN) { bestN = n; best = e.emotion; }
        });
        return best;
    }

    function timeLabel(ts) {
        const d = new Date(ts);
        const today = new Date();
        const sameDay = d.toDateString() === today.toDateString();
        return sameDay
            ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : d.toLocaleDateString([], { day: 'numeric', month: 'short' }) +
              ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function durationLabel(ms) {
        if (!ms || ms < 0) return '';
        const s = Math.round(ms / 1000);
        if (s < 60) return s + 's';
        return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    }

    function preview(entry) {
        if (entry.mode === 'voice') {
            const first = (entry.turns || []).find((t) => t.role === 'user' && t.text.trim());
            return first ? first.text : '(no speech captured)';
        }
        return entry.text || '';
    }

    /* ------------------------- rendering ------------------------- */

    function render() {
        const listEl = document.getElementById('historyList');
        if (!listEl) return;

        const list = read();
        const countEl = document.getElementById('historyCount');
        if (countEl) countEl.textContent = list.length ? String(list.length) : '';

        listEl.textContent = '';

        if (!list.length) {
            const empty = document.createElement('p');
            empty.className = 'history-empty';
            empty.textContent = 'No history yet.';
            listEl.appendChild(empty);
            return;
        }

        list.forEach((entry) => listEl.appendChild(renderEntry(entry)));
    }

    function renderEntry(entry) {
        const isVoice = entry.mode === 'voice';

        const item = document.createElement('div');
        item.className = 'history-item ' + entry.mode;
        item.dataset.id = entry.id;

        // --- header row ---
        const meta = document.createElement('div');
        meta.className = 'history-meta';

        const badge = document.createElement('span');
        badge.className = 'hist-badge ' + entry.mode;
        badge.textContent = isVoice ? '🎙 VOICE' : '⌨ TEXT';
        meta.appendChild(badge);

        const emo = isVoice ? dominantEmotion(entry.emotions) : null;
        if (emo) {
            const chip = document.createElement('span');
            chip.className = 'hist-emotion';
            chip.dataset.emotion = emo;
            chip.textContent = (FACES[emo] || '') + ' ' + emo;
            meta.appendChild(chip);
        } else if (!isVoice && entry.detectedType) {
            const chip = document.createElement('span');
            chip.className = 'hist-emotion';
            chip.textContent = entry.detectedType;
            meta.appendChild(chip);
        }

        const time = document.createElement('span');
        time.className = 'hist-time';
        time.textContent = timeLabel(entry.at) +
            (isVoice && entry.durationMs ? ' · ' + durationLabel(entry.durationMs) : '');
        meta.appendChild(time);

        const del = document.createElement('button');
        del.className = 'hist-del';
        del.type = 'button';
        del.title = 'Delete';
        del.dataset.action = 'delete';
        del.textContent = '×';
        meta.appendChild(del);

        item.appendChild(meta);

        // --- summary (voice sessions) ---
        if (isVoice) {
            if (entry.summary) {
                const sum = document.createElement('div');
                sum.className = 'hist-summary';
                sum.textContent = entry.summary;
                item.appendChild(sum);
            } else if (entry.summaryState === 'pending') {
                const sum = document.createElement('div');
                sum.className = 'hist-summary pending';
                sum.textContent = 'Summarizing…';
                item.appendChild(sum);
            } else if (entry.summaryState === 'failed') {
                const sum = document.createElement('div');
                sum.className = 'hist-summary failed';
                sum.textContent = 'Summary unavailable';
                item.appendChild(sum);
            }
        }

        // --- preview line ---
        const text = document.createElement('div');
        text.className = 'history-text';
        text.textContent = preview(entry);
        item.appendChild(text);

        // --- expandable detail ---
        const hasDetail = isVoice
            ? (entry.turns && entry.turns.length) || (entry.emotions && entry.emotions.length)
            : false;

        if (hasDetail) {
            const toggle = document.createElement('button');
            toggle.className = 'hist-expand';
            toggle.type = 'button';
            toggle.dataset.action = 'expand';
            toggle.textContent = 'Show conversation';
            item.appendChild(toggle);

            const detail = document.createElement('div');
            detail.className = 'hist-detail hidden';

            (entry.turns || []).forEach((t) => {
                const line = document.createElement('div');
                line.className = 'hist-turn ' + t.role;
                const who = document.createElement('span');
                who.className = 'who';
                who.textContent = t.role === 'user' ? 'You' : 'Gemini';
                const body = document.createElement('span');
                body.className = 'body';
                body.textContent = t.text;
                line.append(who, body);
                detail.appendChild(line);
            });

            if (entry.emotions && entry.emotions.length) {
                const head = document.createElement('div');
                head.className = 'hist-detail-head';
                head.textContent = 'Emotion readings';
                detail.appendChild(head);

                entry.emotions.forEach((e) => {
                    const row = document.createElement('div');
                    row.className = 'hist-emo-row';
                    const label = document.createElement('span');
                    label.className = 'hist-emo-label';
                    label.dataset.emotion = e.emotion;
                    const pct = Math.round((e.confidence || 0) * 100);
                    label.textContent = (FACES[e.emotion] || '') + ' ' + e.emotion + ' · ' + pct + '%';
                    row.appendChild(label);
                    if (e.evidence) {
                        const ev = document.createElement('span');
                        ev.className = 'hist-emo-ev';
                        ev.textContent = e.evidence;
                        row.appendChild(ev);
                    }
                    detail.appendChild(row);
                });
            }

            item.appendChild(detail);
        } else if (!isVoice) {
            const reuse = document.createElement('button');
            reuse.className = 'hist-expand';
            reuse.type = 'button';
            reuse.dataset.action = 'reuse';
            reuse.textContent = 'Load into editor';
            item.appendChild(reuse);
        }

        return item;
    }

    /* ------------------------- events ------------------------- */

    function bind() {
        const listEl = document.getElementById('historyList');
        if (listEl) {
            // Delegation, so entry text is never interpolated into an onclick
            // attribute (the old implementation broke on apostrophes).
            listEl.addEventListener('click', (e) => {
                const btn = e.target.closest('[data-action]');
                const item = e.target.closest('.history-item');
                if (!item) return;
                const id = item.dataset.id;

                if (!btn) return;
                const action = btn.dataset.action;

                if (action === 'delete') {
                    remove(id);
                } else if (action === 'expand') {
                    const detail = item.querySelector('.hist-detail');
                    if (!detail) return;
                    const nowHidden = detail.classList.toggle('hidden');
                    btn.textContent = nowHidden ? 'Show conversation' : 'Hide conversation';
                } else if (action === 'reuse') {
                    const entry = read().find((x) => x.id === id);
                    const input = document.getElementById('inputText');
                    if (entry && input) {
                        input.value = entry.text || '';
                        input.dispatchEvent(new Event('input'));
                        const textTab = document.querySelector('.mode-tab[data-mode="text"]');
                        if (textTab) textTab.click();
                        toggle(false);
                        input.focus();
                    }
                }
            });
        }

        const clearBtn = document.getElementById('historyClear');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                if (read().length && confirm('Clear all history?')) clear();
            });
        }

        const toggleBtn = document.querySelector('.history-toggle');
        if (toggleBtn) toggleBtn.addEventListener('click', () => toggle());

        const closeBtn = document.getElementById('historyClose');
        if (closeBtn) closeBtn.addEventListener('click', () => toggle(false));

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') toggle(false);
        });
    }

    function toggle(force) {
        const bar = document.getElementById('historySidebar');
        if (!bar) return;
        const show = typeof force === 'boolean' ? force : !bar.classList.contains('active');
        bar.classList.toggle('active', show);
    }

    /* ------------------------- public API ------------------------- */

    const AISHistory = {
        add: add,
        update: update,
        remove: remove,
        clear: clear,
        all: read,
        render: render,
        toggle: toggle,
        dominantEmotion: dominantEmotion,

        /** Text mode: record one analysis. */
        addTextAnalysis: function (text, detectedType, action) {
            return add({
                mode: 'text',
                text: text,
                detectedType: detectedType || 'unknown',
                action: action || ''
            });
        },

        /** Voice mode: open a session record that gets patched as it runs. */
        startVoiceSession: function (language) {
            return add({
                mode: 'voice',
                language: language,
                turns: [],
                emotions: [],
                durationMs: 0
            });
        }
    };

    window.AISHistory = AISHistory;

    // Back-compat with the call already in script.js.
    window.addToHistory = AISHistory.addTextAnalysis;
    window.toggleHistory = toggle;

    document.addEventListener('DOMContentLoaded', () => {
        bind();
        render();
        load();
    });
    window.addEventListener('pagehide', flush);
})();
