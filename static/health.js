/**
 * Health page: is every connection working right now — and switch each on/off.
 *
 * Every card is a live server-side test (/api/health). The on/off switches
 * drive the same tool groups as the Tools tab, so they are enforced by the
 * server, and the two tabs can never disagree.
 */
(function () {
    'use strict';

    const LABEL = {
        ok: 'Connected', warn: 'Needs attention', error: 'Not working',
        unconfigured: 'Not set up', off: 'Switched off', idle: 'Not checked'
    };

    let data = null;
    let loading = false;
    let changeTimer = null;

    function el(tag, cls, text) {
        const n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = text;
        return n;
    }

    function notifyChanged() {
        clearTimeout(changeTimer);
        changeTimer = setTimeout(() => {
            if (typeof window.AISToolsChanged === 'function') window.AISToolsChanged();
            if (window.AISTools && window.AISTools.reload) window.AISTools.reload();
        }, 900);
    }

    function makeSwitch(checked, label, onChange, partial) {
        const wrap = el('label', 'tsw' + (partial ? ' partial' : ''));
        const input = el('input');
        input.type = 'checkbox';
        input.checked = checked;
        input.setAttribute('aria-label', label);
        input.addEventListener('change', () => onChange(input.checked));
        const track = el('span', 'tsw-track');
        track.appendChild(el('span', 'tsw-thumb'));
        wrap.append(input, track);
        return wrap;
    }

    /* ------------------------------ summary ------------------------------ */

    function renderSummary() {
        const title = document.getElementById('hsTitle');
        const sub = document.getElementById('hsSub');
        const dot = document.getElementById('hsDot');
        const badge = document.getElementById('healthBadge');
        if (!data) return;

        const s = data.summary || {};
        const bad = (s.error || 0) + (s.unconfigured || 0);
        const warn = s.warn || 0;
        const ok = s.ok || 0;
        const off = s.off || 0;
        const active = data.integrations.length - off;

        let state = 'ok';
        if (bad) state = 'error';
        else if (warn) state = 'warn';

        dot.className = 'hs-dot ' + state;
        title.textContent = state === 'ok'
            ? (active ? 'Everything is connected' : 'Everything is switched off')
            : (bad + warn) + ' of ' + active + ' need' + ((bad + warn) === 1 ? 's' : '') +
              ' attention';
        const parts = [ok + ' connected'];
        if (warn) parts.push(warn + ' warning' + (warn === 1 ? '' : 's'));
        if (bad) parts.push(bad + ' not working');
        if (off) parts.push(off + ' off');
        sub.textContent = parts.join(' · ') + ' — checked ' +
            String(data.checked_at || '').slice(11);

        if (badge) {
            badge.textContent = bad ? String(bad) : (warn ? '!' : '');
            badge.className = 'health-badge' + (bad ? ' bad' : warn ? ' warn' : '');
        }
    }

    /* ------------------------------- cards ------------------------------- */

    function statusPill(status) {
        return el('span', 'hpill ' + status, LABEL[status] || status);
    }

    function renderCard(it) {
        const card = el('article', 'hcard ' + it.status);
        card.dataset.id = it.id;

        const head = el('div', 'hcard-head');
        const title = el('div', 'hcard-title');
        title.append(el('span', 'hdot ' + it.status), el('span', 'hicon', it.icon),
                     el('span', 'hname', it.label));
        head.appendChild(title);
        if (it.toggleable) {
            head.appendChild(makeSwitch(it.enabled, it.label,
                (on) => toggle(it.id, on), it.partial));
        } else {
            head.appendChild(el('span', 'hcore', 'core'));
        }
        card.appendChild(head);

        const meta = el('div', 'hcard-meta');
        meta.appendChild(statusPill(it.status));
        if (it.latency_ms != null) meta.appendChild(el('span', 'hlat', it.latency_ms + ' ms'));
        if (it.partial) meta.appendChild(el('span', 'hlat', 'partly on — see Tools'));
        card.appendChild(meta);

        card.appendChild(el('p', 'hdetail', it.detail || ''));

        // integration-specific extras
        if (it.id === 'email' && it.status !== 'off' && it.extra) renderEmail(card, it.extra);
        if (it.id === 'folders' && it.extra && it.extra.roots) renderFolders(card, it.extra.roots);
        if (it.extra && it.extra.note) card.appendChild(el('p', 'hnote', it.extra.note));

        const foot = el('div', 'hcard-foot');
        const again = el('button', 'hlink', '↻ Recheck');
        again.type = 'button';
        again.addEventListener('click', () => recheck(it.id, card));
        foot.appendChild(again);
        card.appendChild(foot);
        return card;
    }

    function renderFolders(card, roots) {
        const list = el('div', 'hsub');
        roots.forEach((r) => {
            const ok = r.exists && r.writable;
            const row = el('div', 'hsub-row');
            row.append(el('span', 'hdot ' + (ok ? 'ok' : 'error')),
                       el('span', 'hsub-name', r.name));
            const p = el('span', 'hsub-path', r.path);
            p.title = r.path;
            row.appendChild(p);
            list.appendChild(row);
        });
        card.appendChild(list);
    }

    /** Gmail connected directly with an app password. */
    function renderGmail(card, x) {
        const list = el('div', 'hsub');
        (x.accounts || []).forEach((a) => {
            const row = el('div', 'hsub-row active');
            row.append(el('span', 'hdot ' + (x.can_send ? 'ok' : 'warn')),
                       el('span', 'hsub-name', a + ' · Gmail direct'));
            list.appendChild(row);
        });
        list.appendChild(el('div', 'hsub-detail', x.can_send
            ? 'Reading and sending go straight to Gmail — Outlook does not need to be open. ' +
              'To disconnect, delete the app password in .env or revoke it at ' +
              'myaccount.google.com/apppasswords.'
            : '⚠ Reading works, but sending failed: ' + (x.send_problem || 'unknown error')));
        card.appendChild(list);
    }

    /** Steps to connect Gmail directly, while it isn't (or its password stopped working). */
    function gmailSetup(list, x) {
        if (!x.gmail || x.gmail === 'connected') return;
        const head = x.gmail === 'error'
            ? '⚠ Gmail direct isn\u2019t working: ' + (x.gmail_error || '') + ' Using Outlook meanwhile.'
            : 'Automatic sending: connect Gmail directly with an app password (2 minutes):';
        list.appendChild(el('div', 'hsub-detail', head));
        const steps = el('ol', 'hsteps');
        (x.steps || []).forEach((t) => steps.appendChild(el('li', null, t)));
        list.appendChild(steps);
    }

    function renderEmail(card, x) {
        if (x.provider === 'gmail') { renderGmail(card, x); return; }
        const list = el('div', 'hsub');
        gmailSetup(list, x);
        (x.accounts || []).forEach((a) => {
            const row = el('div', 'hsub-row active');
            row.append(el('span', 'hdot ' + (x.synced === false ? 'error'
                                              : x.unlicensed ? 'warn'
                                              : (x.problems || []).length ? 'error'
                                              : x.synced ? 'ok' : 'idle')),
                       el('span', 'hsub-name', a + ' · Outlook app'));
            list.appendChild(row);
        });
        (x.problems || []).forEach((p) => list.appendChild(el('div', 'hsub-detail', '⚠ ' + p)));

        if (x.unlicensed) {
            list.appendChild(el('div', 'hsub-detail',
                'Sending: bena opens the finished email in Gmail — you press Send. ' +
                'Connecting Gmail directly (above) makes it automatic; so would an Office licence:'));
            const steps = el('ol', 'hsteps');
            [
                'Open Outlook (button below).',
                'File → Office Account  (קובץ ← חשבון Office).',
                'Click Sign in and use the Microsoft account that has your Microsoft 365 licence — ' +
                    'usually your school or work account (not the Gmail one).',
                'Close and reopen Outlook. The title bar should no longer say "Unlicensed Product" ' +
                    '(מוצר ללא רשיון). Then Recheck.'
            ].forEach((t) => steps.appendChild(el('li', null, t)));
            list.appendChild(steps);
        } else if (x.fix) {
            const steps = el('ol', 'hsteps');
            [
                'Open Outlook (button below).',
                'File → Account Settings → Account Settings  (קובץ ← הגדרות חשבון).',
                'Select the Gmail account and Remove it — it has no mail downloaded, nothing is lost.',
                'Add Account → type your Gmail address → Connect → choose "Sign in with Google".',
                'Allow Outlook in the Google window, wait for the folders to appear, then Recheck.'
            ].forEach((t) => steps.appendChild(el('li', null, t)));
            list.appendChild(steps);
        }

        const actions = el('div', 'hactions');
        const open = el('button', 'hlink', x.fix ? 'Open Outlook to fix it' : 'Open Outlook');
        open.type = 'button';
        open.addEventListener('click', async () => {
            open.disabled = true;
            open.textContent = 'Opening Outlook…';
            await fetch('/api/outlook/open', { method: 'POST' }).catch(() => {});
            open.textContent = 'Outlook opened — finish there, then Recheck';
        });
        actions.appendChild(open);
        if (!x.running) {
            const chk = el('button', 'hlink', 'Check the mailbox (opens Outlook)');
            chk.type = 'button';
            chk.addEventListener('click', () => probeOutlook(chk));
            actions.appendChild(chk);
        }
        list.appendChild(actions);
        card.appendChild(list);
    }

    function render() {
        const grid = document.getElementById('healthGrid');
        if (!grid || !data) return;
        grid.textContent = '';
        data.integrations.forEach((it) => grid.appendChild(renderCard(it)));
        renderSummary();
    }

    function renderSkeleton() {
        const grid = document.getElementById('healthGrid');
        if (!grid || data) return;
        grid.textContent = '';
        for (let i = 0; i < 6; i++) {
            const c = el('article', 'hcard loading');
            c.append(el('div', 'hskel w60'), el('div', 'hskel w40'), el('div', 'hskel w80'));
            grid.appendChild(c);
        }
    }

    /* ------------------------------ actions ------------------------------ */

    function merge(fresh) {
        if (!data) { data = fresh; return; }
        fresh.integrations.forEach((f) => {
            const i = data.integrations.findIndex((x) => x.id === f.id);
            if (i >= 0) data.integrations[i] = f; else data.integrations.push(f);
        });
        const counts = {};
        data.integrations.forEach((x) => { counts[x.status] = (counts[x.status] || 0) + 1; });
        data.summary = counts;
        data.checked_at = fresh.checked_at;
    }

    async function load() {
        if (loading) return;
        loading = true;
        renderSkeleton();
        const btn = document.getElementById('healthRefresh');
        if (btn) btn.classList.add('spinning');
        try {
            data = await fetch('/api/health').then((r) => r.json());
            render();
        } catch (e) {
            const t = document.getElementById('hsTitle');
            if (t) t.textContent = 'Could not reach the app server.';
        } finally {
            loading = false;
            if (btn) btn.classList.remove('spinning');
        }
    }

    async function recheck(id, card) {
        if (card) card.classList.add('checking');
        try {
            merge(await fetch('/api/health/' + id).then((r) => r.json()));
            render();
        } catch (_) {
            if (card) card.classList.remove('checking');
        }
    }

    async function toggle(id, on) {
        try {
            const res = await fetch('/api/integrations', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id, enabled: on })
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            merge(await res.json());
            render();
            notifyChanged();
        } catch (e) {
            await load();          // resync with what the server really has
        }
    }

    async function probeOutlook(btn) {
        btn.disabled = true;
        btn.textContent = 'Opening Outlook… (up to 15s)';
        try {
            await fetch('/api/health/outlook-probe', { method: 'POST' });
        } catch (_) { /* the recheck below reports it */ }
        await recheck('email');
    }

    document.addEventListener('DOMContentLoaded', () => {
        const btn = document.getElementById('healthRefresh');
        if (btn) btn.addEventListener('click', load);
        const tab = document.querySelector('.mode-tab[data-mode="health"]');
        if (tab) tab.addEventListener('click', load);
        // A quiet first pass so the tab badge can flag problems early.
        setTimeout(load, 1500);
    });

    window.AISHealth = { reload: load };
})();
