/**
 * Brain tab: memory, routines, skills and past conversations.
 * Everything she keeps about you is visible and editable here.
 */
(function () {
    'use strict';

    let data = null;

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    async function post(url, body) {
        const r = await fetch(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        return r.json();
    }

    function btn(label, cls, onClick) {
        const b = el('button', cls || 'blink', label);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    /* ------------------------------- memory ------------------------------ */

    function renderMemory() {
        const host = document.getElementById('brainMemory');
        host.textContent = '';
        [['user', 'About you'], ['memory', 'Her own notes']].forEach(([key, label]) => {
            const m = data.memory[key];
            const col = el('div', 'bmem-col');
            const head = el('div', 'bmem-head');
            head.append(el('span', 'bmem-title', label),
                        el('span', 'bmem-use', m.used + ' / ' + m.limit + ' chars'));
            col.appendChild(head);
            const bar = el('div', 'bbar');
            const fill = el('div', 'bbar-fill' + (m.percent >= 80 ? ' hot' : ''));
            fill.style.width = Math.min(100, m.percent) + '%';
            bar.appendChild(fill);
            col.appendChild(bar);

            const list = el('ul', 'bmem-list');
            if (!m.entries.length) {
                list.appendChild(el('li', 'bempty', key === 'user'
                    ? 'Nothing yet — tell her your name or how you like answers.'
                    : 'Nothing yet — she adds notes as she learns about this computer.'));
            }
            m.entries.forEach((entry) => {
                const li = el('li', 'bmem-item');
                li.appendChild(el('span', 'bmem-text', entry));
                li.appendChild(btn('✕', 'bx', async () => {
                    await post('/api/brain/memory', {
                        action: 'remove', target: key, old_text: entry
                    });
                    load();
                }));
                list.appendChild(li);
            });
            col.appendChild(list);

            const add = el('div', 'badd');
            const input = el('input');
            input.placeholder = key === 'user' ? 'Add something about you…' : 'Add a note for her…';
            const msg = el('span', 'bmsg');
            const go = btn('Add', 'mini-btn', async () => {
                if (!input.value.trim()) return;
                const r = await post('/api/brain/memory', {
                    action: 'add', target: key, content: input.value
                });
                if (r.ok) { input.value = ''; load(); } else { msg.textContent = r.error; }
            });
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click(); });
            add.append(input, go);
            col.append(add, msg);
            host.appendChild(col);
        });
    }

    /* ------------------------------ routines ----------------------------- */

    function renderRoutines() {
        const host = document.getElementById('brainRoutines');
        host.textContent = '';
        if (!data.routines.length) {
            host.appendChild(el('p', 'bempty', 'No routines yet. Ask her — "every morning at 8, ' +
                'tell me the weather and my calendar" — or add one below.'));
        }
        data.routines.forEach((j) => {
            const card = el('div', 'broutine' + (j.enabled ? '' : ' off'));
            const top = el('div', 'brow');
            const name = el('div', 'bname');
            name.append(el('strong', null, j.name), el('span', 'bwhen', j.when || j.schedule));
            top.appendChild(name);

            const sw = el('label', 'tsw');
            const cb = el('input'); cb.type = 'checkbox'; cb.checked = !!j.enabled;
            cb.setAttribute('aria-label', 'Enable ' + j.name);
            cb.addEventListener('change', async () => {
                const r = await post('/api/routines', {
                    action: cb.checked ? 'enable' : 'disable', name: j.id
                });
                if (!r.ok) alert(r.error);
                load();
            });
            const track = el('span', 'tsw-track'); track.appendChild(el('span', 'tsw-thumb'));
            sw.append(cb, track);
            top.appendChild(sw);
            card.appendChild(top);

            card.appendChild(el('p', 'bprompt', j.prompt + (j.skill ? '  · skill: ' + j.skill : '')));
            const meta = el('div', 'bmeta');
            meta.append(el('span', null, j.running ? '⏳ running now'
                : (j.enabled && j.next_run ? 'Next: ' + j.next_run : 'Paused')));
            if (j.last_run) {
                meta.append(el('span', 'bstat ' + (j.last_status || ''),
                    'Last: ' + j.last_run + ' · ' + (j.last_status || '')));
            }
            card.appendChild(meta);
            if (j.last_result) card.appendChild(el('p', 'bresult', j.last_result));

            const acts = el('div', 'bacts');
            acts.append(
                btn('▶ Run now', 'blink', async () => {
                    await post('/api/routines', { action: 'run', name: j.id });
                    setTimeout(load, 600);
                    setTimeout(load, 8000);
                }),
                btn('Delete', 'blink danger', async () => {
                    if (!confirm('Delete the routine "' + j.name + '"?')) return;
                    await post('/api/routines', { action: 'delete', name: j.id });
                    load();
                })
            );
            card.appendChild(acts);
            host.appendChild(card);
        });

        const sel = document.getElementById('rtSkill');
        const keep = sel.value;
        sel.textContent = '';
        sel.appendChild(new Option('No skill', ''));
        data.skills.forEach((s) => sel.appendChild(new Option('Skill: ' + s.name, s.name)));
        sel.value = keep;

        const feed = document.getElementById('brainFeed');
        feed.textContent = '';
        if (data.feed.length) {
            feed.appendChild(el('h3', 'bh3', 'Recent results'));
            data.feed.slice(0, 5).forEach((f) => {
                const row = el('div', 'bfeed ' + f.status);
                row.append(el('span', 'bfeed-when', f.at + ' · ' + f.name),
                           el('span', 'bfeed-text', f.status === 'silent'
                               ? '(nothing to report)' : f.text));
                feed.appendChild(row);
            });
        }
        const s = data.scheduler;
        feed.appendChild(el('p', 'bhint', s.active
            ? 'Scheduler running here · model ' + s.model
            : (s.last_tick ? 'Another bena window is running the scheduler.'
                           : 'Scheduler starts with the app.')));
    }

    async function createRoutine() {
        const msg = document.getElementById('rtMsg');
        const r = await post('/api/routines', {
            action: 'create',
            name: document.getElementById('rtName').value,
            schedule: document.getElementById('rtSchedule').value,
            prompt: document.getElementById('rtPrompt').value,
            skill: document.getElementById('rtSkill').value || null
        });
        msg.textContent = r.ok ? r.message : r.error;
        msg.className = 'bmsg ' + (r.ok ? 'ok' : 'err');
        if (r.ok) {
            ['rtName', 'rtSchedule', 'rtPrompt'].forEach((id) => { document.getElementById(id).value = ''; });
            load();
        }
    }

    /* ------------------------------- skills ------------------------------ */

    function renderSkills() {
        const host = document.getElementById('brainSkills');
        host.textContent = '';
        if (!data.skills.length) host.appendChild(el('p', 'bempty', 'No skills yet.'));
        data.skills.forEach((s) => {
            const d = el('details', 'bskill');
            const sum = el('summary');
            sum.append(el('strong', null, s.name), el('span', 'bwhen', 'v' + s.version),
                       el('span', 'bdesc', s.description));
            d.appendChild(sum);
            d.appendChild(el('pre', 'bsteps', s.steps));
            d.appendChild(btn('Delete skill', 'blink danger', async () => {
                if (!confirm('Delete the skill "' + s.name + '"?')) return;
                await post('/api/brain/skill', { action: 'delete', name: s.name });
                load();
            }));
            host.appendChild(d);
        });
    }

    /* ---------------------------- conversations -------------------------- */

    function renderSessions() {
        const s = data.sessions;
        document.getElementById('brainSessions').textContent =
            s.sessions + ' conversation' + (s.sessions === 1 ? '' : 's') + ' saved (' +
            s.messages + ' message' + (s.messages === 1 ? '' : 's') + '), stored in brain.db and saved ' +
            'while you talk. She searches these when you mention something from before. ' +
            'Switching Memory off in Tools stops her searching new ones; the history sidebar still keeps them.';
    }

    async function search() {
        const q = document.getElementById('bsQuery').value;
        const host = document.getElementById('bsResults');
        host.textContent = '';
        const r = await fetch('/api/brain/search?q=' + encodeURIComponent(q)).then((x) => x.json());
        if (!r.ok) { host.appendChild(el('p', 'bmsg err', r.error)); return; }
        if (!r.matches.length) { host.appendChild(el('p', 'bempty', 'Nothing found.')); return; }
        r.matches.forEach((m) => {
            const row = el('div', 'bfeed');
            row.append(el('span', 'bfeed-when', (m.when || '?') + ' · ' + (m.who === 'user' ? 'You' : 'bena')),
                       el('span', 'bfeed-text', m.text));
            host.appendChild(row);
        });
    }

    /* -------------------------------- load ------------------------------- */

    async function load() {
        try {
            data = await fetch('/api/brain').then((r) => r.json());
        } catch (_) { return; }
        renderMemory();
        renderRoutines();
        renderSkills();
        renderSessions();
    }

    document.addEventListener('DOMContentLoaded', () => {
        const tab = document.querySelector('.mode-tab[data-mode="brain"]');
        if (tab) tab.addEventListener('click', load);
        document.getElementById('rtCreate').addEventListener('click', createRoutine);
        document.getElementById('bsGo').addEventListener('click', search);
        document.getElementById('bsQuery').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') search();
        });
    });

    window.AISBrain = { reload: load };
})();
