/**
 * Desktop shell mode.
 *
 * Only active when the page is opened as /?shell=desktop by desktop.py.
 * The page is the assistant PANEL; the orb is a separate circular window
 * (orb.py). Clicking the orb shows/hides this panel — the panel itself never
 * turns into an orb. In a normal browser tab none of this runs.
 */
(function () {
    'use strict';

    if (new URLSearchParams(location.search).get('shell') !== 'desktop') return;

    // Always laid out as the full panel; hiding is done by the window itself.
    document.documentElement.classList.add('desktop-shell', 'expanded');

    const api = () => (window.pywebview && window.pywebview.api) || null;

    document.addEventListener('DOMContentLoaded', () => {
        const bar = document.createElement('div');
        bar.className = 'shell-bar';
        bar.innerHTML =
            '<span class="shell-drag pywebview-drag-region" title="Drag to move">' +
            '<i class="fas fa-grip-lines pywebview-drag-region"></i></span>' +
            '<button class="shell-btn" id="shellHistory" title="History — past conversations and results">' +
            '<i class="fas fa-history"></i></button>' +
            '<button class="shell-btn" id="shellCollapse" title="Hide — click the orb to open again">' +
            '<i class="fas fa-minus"></i></button>' +
            '<button class="shell-btn danger" id="shellQuit" title="Quit">' +
            '<i class="fas fa-xmark"></i></button>';
        document.body.insertBefore(bar, document.body.firstChild);

        let expanded = false;       // the window starts hidden

        function setExpanded(next) {
            expanded = next;
            const a = api();
            if (a && a.toggle) a.toggle(expanded);
        }

        document.getElementById('shellHistory')
            .addEventListener('click', () => { if (window.toggleHistory) window.toggleHistory(); });
        document.getElementById('shellCollapse')
            .addEventListener('click', () => setExpanded(false));
        document.getElementById('shellQuit')
            .addEventListener('click', () => { const a = api(); if (a) a.quit(); });

        /* ---------------- wake word, detected in Python ----------------
         * WebView2 ships SpeechRecognition but never starts it, so the
         * browser hotword is dead here. The desktop build detects the phrase
         * server-side with Vosk (offline, no API key) and we poll for it.
         */
        const wakeToggle = document.getElementById('wakeToggle');
        const wakeStatus = document.getElementById('wakeStatus');
        const wakePhraseInput = document.getElementById('wakePhraseInput');
        const talkBtnEl = document.getElementById('talkBtn');

        let armed = false;
        let polling = null;

        function wakePhrase() {
            const v = wakePhraseInput && wakePhraseInput.value.trim();
            return v || 'hey gemini';
        }

        function setWake(msg, cls) {
            if (!wakeStatus) return;
            wakeStatus.textContent = msg;
            wakeStatus.className = 'wake-status' + (cls ? ' ' + cls : '');
        }

        async function arm() {
            setWake('Loading offline speech model…', 'on');
            const lang = localStorage.getItem('ais_voice_lang') || 'en';
            const res = await fetch('/api/wake/arm', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phrase: wakePhrase(), lang: lang })
            }).then((r) => r.json()).catch(() => ({ ok: false, error: 'server unreachable' }));

            if (!res.ok) {
                setWake(res.error || 'Could not start the wake word.', 'err');
                if (wakeToggle) wakeToggle.checked = false;
                armed = false;
                return;
            }
            armed = true;
            // A notice (e.g. "no Hebrew model, using the English phrase") is
            // informational — it must not read as, or behave like, a failure.
            setWake(res.notice || ('Listening for "' + res.phrase + '" (offline)'),
                    res.notice ? 'woke' : 'on');
            if (wakePhraseInput && res.phrase) wakePhraseInput.value = res.phrase;
            if (!polling) polling = setInterval(poll, 400);
        }

        async function disarm() {
            armed = false;
            clearInterval(polling);
            polling = null;
            await fetch('/api/wake/disarm', { method: 'POST' }).catch(() => {});
            setWake('');
        }

        async function poll() {
            if (!armed) return;
            const d = await fetch('/api/wake/poll').then((r) => r.json()).catch(() => null);
            if (!d) return;
            if (d.error) { setWake(d.error, 'err'); return; }

            const busy = talkBtnEl && talkBtnEl.classList.contains('live');
            if (d.triggered && !busy) {
                setWake('Heard you.', 'woke');

                // Stay a circle. The orb turns green on its own (it mirrors
                // talkBtn.live), so there is nothing to open — you just talk.
                const voiceTab = document.querySelector('.mode-tab[data-mode="voice"]');
                if (voiceTab && !voiceTab.classList.contains('active')) voiceTab.click();

                // Release the mic before the Live session takes it.
                fetch('/api/wake/pause', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ paused: true })
                }).catch(() => {});

                // greet:true makes her open with "How can I help you?"
                if (typeof window.AISStartVoiceSession === 'function') {
                    window.AISStartVoiceSession({ greet: true });
                } else if (talkBtnEl) {
                    talkBtnEl.click();
                }
            } else if (!d.triggered && d.heard) {
                setWake('heard: ' + d.heard, 'on');
            }
        }

        // Keep the orb process in step: give the mic back when a call ends,
        // and tell the orb which colour to be (it is a separate process).
        let lastOrbState = null;
        function syncOrb(state) {
            if (state === lastOrbState) return;
            lastOrbState = state;
            fetch('/api/shell/orb', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ state: state })
            }).catch(() => {});
        }

        if (talkBtnEl) {
            new MutationObserver(() => {
                const live = talkBtnEl.classList.contains('live');
                syncOrb(live ? 'live' : (armed ? 'idle' : 'idle'));
                if (armed && !live) {
                    fetch('/api/wake/pause', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ paused: false })
                    }).catch(() => {});
                }
            }).observe(talkBtnEl, { attributes: true, attributeFilter: ['class'] });
        }

        // Orb clicks are handled in desktop.py (Python), which shows/hides this window.

        if (wakeToggle) {
            wakeToggle.disabled = false;
            wakeToggle.addEventListener('change', () => {
                // live-ui.js owns this key in the browser build but is
                // suppressed here, so the desktop shell must persist it
                // itself — otherwise switching hands-free off never sticks.
                localStorage.setItem('ais_wake_on', wakeToggle.checked ? '1' : '0');
                if (wakeToggle.checked) arm(); else disarm();
            }, true);
            // Hands-free is the whole point of the orb, so default it on.
            if (localStorage.getItem('ais_wake_on') !== '0') {
                wakeToggle.checked = true;
                arm();
            }
        }
        window.addEventListener('beforeunload', () => {
            navigator.sendBeacon && navigator.sendBeacon('/api/wake/disarm');
        });

    });
})();
