/**
 * Wires LiveSession (live.js) to the voice-mode DOM.
 */
document.addEventListener('DOMContentLoaded', () => {
    const tabs = document.querySelectorAll('.mode-tab');
    const textMode = document.getElementById('textMode');
    const voiceMode = document.getElementById('voiceMode');

    const talkBtn = document.getElementById('talkBtn');
    const muteBtn = document.getElementById('muteBtn');
    const endBtn = document.getElementById('endBtn');
    const statusEl = document.getElementById('voiceStatus');

    const emotionCard = document.getElementById('emotionCard');
    const emotionFace = document.getElementById('emotionFace');
    const emotionName = document.getElementById('emotionName');
    const emotionBar = document.getElementById('emotionBar');
    const emotionConf = document.getElementById('emotionConf');
    const emotionEnergy = document.getElementById('emotionEnergy');
    const emotionEvidence = document.getElementById('emotionEvidence');
    const emotionTime = document.getElementById('emotionTime');

    const transcriptEl = document.getElementById('transcript');

    const langSwitch = document.getElementById('langSwitch');
    const langBarText = document.getElementById('langBarText');

    const wakeToggle = document.getElementById('wakeToggle');
    const wakeStatus = document.getElementById('wakeStatus');
    const wakePhrase = document.getElementById('wakePhrase');
    const wakePhraseInput = document.getElementById('wakePhraseInput');
    const wakePresets = document.getElementById('wakePresets');
    const wakeSensitivity = document.getElementById('wakeSensitivity');
    const wakeHeard = document.getElementById('wakeHeard');
    const wakeHeardText = document.getElementById('wakeHeardText');
    const wakeHeardScore = document.getElementById('wakeHeardScore');

    const FACES = {
        neutral: '😐', happy: '😊', excited: '🤩', calm: '😌',
        sad: '😔', frustrated: '😤', angry: '😠',
        anxious: '😰', confused: '😕', tired: '😴'
    };

    const LANGS = window.LIVE_LANGUAGES;
    const LANG_KEY = 'ais_voice_lang';
    const BAR_LABEL = { he: 'שפת הדיבור', en: 'Voice language' };

    let session = null;
    let voiceEnabled = true;
    let swapping = false;   // true while tearing down + reopening for a language change

    // wake word
    let wake = null;
    let wakeWanted = false;

    // current session's history record
    let histId = null;
    let sessionTurns = [];
    let sessionEmotions = [];
    let sessionStart = 0;

    let language = localStorage.getItem(LANG_KEY);
    if (!LANGS[language]) language = window.LIVE_DEFAULT_LANGUAGE;

    /* ---------------- mode switching ---------------- */

    tabs.forEach((tab) => {
        tab.addEventListener('click', () => {
            tabs.forEach((t) => t.classList.remove('active'));
            tab.classList.add('active');
            const mode = tab.dataset.mode;
            ['text', 'voice', 'tools', 'brain', 'health'].forEach((m) => {
                const pane = document.getElementById(m + 'Mode');
                if (pane) pane.classList.toggle('hidden', m !== mode);
            });
            // Never keep the wake recogniser on the mic while on the Text tab —
            // it would fight the text-mode dictation button for it.
            syncWake();
        });
    });

    /* ---------------- voice language ---------------- */

    function renderLanguage() {
        langSwitch.querySelectorAll('.lang-opt').forEach((btn) => {
            const on = btn.dataset.lang === language;
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        langBarText.textContent = BAR_LABEL[language] || BAR_LABEL.en;
        renderWakePhrase();
        // Recognise the wake word in that language, with that language's phrase.
        if (wake) wake.setLang(language, wakePhraseFor(language));

        // Idle greeting follows the chosen language.
        if (!session && voiceEnabled) {
            statusEl.textContent = LANGS[language].ui.greeting;
            statusEl.className = 'voice-status';
        }
    }

    async function setLanguage(next) {
        if (!LANGS[next] || next === language) return;
        language = next;
        localStorage.setItem(LANG_KEY, language);
        renderLanguage();

        // The Live API only reads systemInstruction from the one-shot setup
        // message, and this model does not support speechConfig.languageCode.
        // So changing language mid-call means reopening the session.
        if (session) {
            swapping = true;
            setStatus('connecting', LANGS[language].ui.switched);
            markTranscript(LANGS[language].flag + ' ' + LANGS[language].label);
            try {
                await endSession();
                await startSession();
            } finally {
                swapping = false;
            }
        }
    }

    langSwitch.addEventListener('click', (e) => {
        const btn = e.target.closest('.lang-opt');
        if (btn) setLanguage(btn.dataset.lang);
    });

    /* ---------------- availability check ---------------- */

    fetch('/api/voice-status')
        .then((r) => r.json())
        .then((d) => {
            voiceEnabled = !!d.enabled;
            if (!voiceEnabled) {
                talkBtn.disabled = true;
                setStatus('error', 'Voice mode is off — add GEMINI_API_KEY to .env and restart the server.');
                if (wakeToggle) wakeToggle.disabled = true;
                setWakeStatus('Needs a Gemini key.', 'err');
                syncWake();
            }
        })
        .catch(() => { /* leave enabled; the click will surface any real error */ });

    /* ---------------- transcript ---------------- */

    // Transcriptions stream in as fragments, so we append into the last
    // bubble from the same speaker instead of making one bubble per fragment.
    let lastRole = null;
    let lastBubble = null;

    function appendTranscript(role, text) {
        const empty = transcriptEl.querySelector('.transcript-empty');
        if (empty) empty.remove();

        if (role !== lastRole || !lastBubble) {
            lastBubble = document.createElement('div');
            lastBubble.className = 'bubble ' + role;
            const who = document.createElement('span');
            who.className = 'who';
            who.textContent = role === 'user' ? 'You' : 'Gemini';
            const body = document.createElement('span');
            body.className = 'body';
            lastBubble.append(who, body);
            transcriptEl.appendChild(lastBubble);
            lastRole = role;
        }
        lastBubble.querySelector('.body').textContent += text;
        transcriptEl.scrollTop = transcriptEl.scrollHeight;

        // Mirror into the history record, grouped the same way.
        const last = sessionTurns[sessionTurns.length - 1];
        if (last && last.role === role) {
            last.text += text;
        } else {
            sessionTurns.push({ role: role, text: text });
        }
        saveProgress();
    }

    /** Write the call so far to the database, so closing the app mid-call
     *  doesn't lose it. history.js coalesces these into one save per ~0.4 s. */
    function saveProgress() {
        if (!histId || !window.AISHistory) return;
        window.AISHistory.update(histId, {
            turns: sessionTurns.map((t) => ({ role: t.role, text: t.text })),
            emotions: sessionEmotions.slice(),
            durationMs: sessionStart ? Date.now() - sessionStart : 0
        });
    }

    function breakTranscript() { lastRole = null; lastBubble = null; }

    /** Horizontal rule in the transcript, e.g. when the language changes. */
    function markTranscript(label) {
        const empty = transcriptEl.querySelector('.transcript-empty');
        if (empty) empty.remove();
        const mark = document.createElement('div');
        mark.className = 'transcript-mark';
        mark.textContent = label;
        transcriptEl.appendChild(mark);
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
        breakTranscript();
    }

    /* ---------------- status ---------------- */

    function setStatus(state, message) {
        statusEl.textContent = message;
        statusEl.className = 'voice-status ' + state;
        talkBtn.classList.toggle('live', state === 'live');
        talkBtn.classList.toggle('connecting', state === 'connecting');
    }

    /* ---------------- emotion panel ---------------- */

    function renderEmotion(args) {
        const emotion = (args.emotion || 'neutral').toLowerCase();
        const confidence = typeof args.confidence === 'number' ? args.confidence : 0;
        const pct = Math.round(Math.max(0, Math.min(1, confidence)) * 100);

        emotionCard.classList.remove('hidden');
        emotionCard.dataset.emotion = emotion;
        emotionFace.textContent = FACES[emotion] || '🙂';
        emotionName.textContent = emotion;
        emotionBar.style.width = pct + '%';
        emotionConf.textContent = 'confidence ' + pct + '%';
        emotionEvidence.textContent = args.evidence || '';
        emotionTime.textContent = new Date().toLocaleTimeString();

        if (args.energy) {
            emotionEnergy.textContent = args.energy + ' energy';
            emotionEnergy.dataset.energy = args.energy;
            emotionEnergy.style.display = '';
        } else {
            emotionEnergy.style.display = 'none';
        }

        // Re-trigger the pulse animation on each new reading.
        emotionCard.classList.remove('pulse');
        void emotionCard.offsetWidth;
        emotionCard.classList.add('pulse');
    }

    /* ---------------- audible cue ---------------- */

    // A short two-note rise, synthesised so there is no asset to ship.
    // This fires the instant the wake word matches — the Gemini greeting
    // cannot, because the socket still has to connect (1-2s), and that silent
    // gap is exactly what makes a wake word feel broken.
    let cueCtx = null;

    function cueContext() {
        if (!cueCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            cueCtx = new Ctx();
        }
        if (cueCtx.state === 'suspended') cueCtx.resume();
        return cueCtx;
    }

    function playChime(notes) {
        const ctx = cueContext();
        if (!ctx) return;
        const start = ctx.currentTime + 0.01;

        notes.forEach((n, i) => {
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.value = n.hz;

            const t0 = start + i * 0.085;
            const t1 = t0 + (n.dur || 0.12);
            // Soft attack/release — a raw gate would click.
            gain.gain.setValueAtTime(0.0001, t0);
            gain.gain.exponentialRampToValueAtTime(0.16, t0 + 0.012);
            gain.gain.exponentialRampToValueAtTime(0.0001, t1);

            osc.connect(gain).connect(ctx.destination);
            osc.start(t0);
            osc.stop(t1 + 0.02);
        });
    }

    const cueWake = () => playChime([{ hz: 880 }, { hz: 1320, dur: 0.16 }]);
    const cueEnd = () => playChime([{ hz: 660 }, { hz: 440, dur: 0.16 }]);

    /* ---------------- wake word ---------------- */

    const WW = window.WakeWordListener;

    /** Wake phrases are stored per language, so HE and EN can differ. */
    function phraseKey(lang) { return 'ais_wake_phrase_' + lang; }

    function wakePhraseFor(lang) {
        const saved = (localStorage.getItem(phraseKey(lang)) || '').trim();
        if (saved) return saved;
        return WW ? (WW.DEFAULT_PHRASE[lang] || WW.DEFAULT_PHRASE.en) : 'hey gemini';
    }

    function currentSensitivity() {
        const saved = localStorage.getItem('ais_wake_sens');
        if (WW && WW.THRESHOLDS[saved]) return saved;
        return WW ? WW.DEFAULT_SENSITIVITY : 'loose';
    }

    function renderWakePhrase() {
        const phrase = wakePhraseFor(language);
        if (wakePhrase) wakePhrase.textContent = '"' + phrase + '"';
        if (wakePhraseInput && wakePhraseInput.value !== phrase) wakePhraseInput.value = phrase;

        // Presets follow the selected language.
        if (wakePresets && WW) {
            wakePresets.textContent = '';
            (WW.PRESETS[language] || WW.PRESETS.en).forEach((p) => {
                const opt = document.createElement('option');
                opt.value = p;
                wakePresets.appendChild(opt);
            });
        }
    }

    /** Live readout of what the recogniser actually heard, and how close it got. */
    function showHeard(text, score, threshold) {
        if (!wakeHeard) return;
        wakeHeard.classList.remove('hidden');
        wakeHeardText.textContent = text.length > 60 ? text.slice(-60) : text;
        const pct = Math.round(score * 100);
        wakeHeardScore.textContent = pct + '%';
        wakeHeardScore.classList.toggle('near', score >= threshold - 0.12 && score < threshold);
        wakeHeardScore.classList.toggle('hit', score >= threshold);
    }

    function setWakeStatus(text, cls) {
        if (!wakeStatus) return;
        wakeStatus.textContent = text || '';
        wakeStatus.className = 'wake-status' + (cls ? ' ' + cls : '');
    }

    // In the desktop shell the wake word is detected in Python (Vosk), because
    // WebView2 ships SpeechRecognition without a working backend. desktop.js
    // owns the toggle there, so the browser listener must stay out of the way.
    const DESKTOP_SHELL = document.documentElement.classList.contains('desktop-shell');

    function ensureWake() {
        if (DESKTOP_SHELL) return null;
        if (wake) return wake;
        if (!WW || !WW.supported) return null;

        wake = new WW({
            lang: language,
            phrase: wakePhraseFor(language),
            threshold: WW.THRESHOLDS[currentSensitivity()],
            onHeard: (text, score, threshold) => showHeard(text, score, threshold),
            onWake: () => {
                cueWake();                       // instant "I heard you"
                setWakeStatus(LANGS[language].ui.woke, 'woke');
                // Hand the microphone over: the recogniser must release it
                // before the Live session grabs it.
                wake.stop({ keepIntent: true });
                if (wakeHeard) wakeHeard.classList.add('hidden');
                // greet:true makes Gemini open with a spoken "yes?" once the
                // socket is up, which confirms it is actually listening.
                if (!session) startSession({ greet: true });
            },
            onStatus: (state, message) => {
                if (state === 'listening') setWakeStatus('Waiting for wake word…', 'on');
                else if (state === 'error') {
                    setWakeStatus(message, 'err');
                    wakeWanted = false;
                    if (wakeToggle) wakeToggle.checked = false;
                } else if (state === 'idle' && !wakeWanted) setWakeStatus('');
            }
        });
        return wake;
    }

    /** Wake listening only makes sense while idle on the Voice tab. */
    function syncWake() {
        const w = ensureWake();
        if (!w) return;
        const voiceTabActive = !voiceMode.classList.contains('hidden');
        const shouldListen = wakeWanted && voiceTabActive && !session && voiceEnabled;

        if (shouldListen) {
            if (!w.wantRunning || !w.running) w.start();
        } else if (w.wantRunning) {
            w.stop({ keepIntent: true });
            if (wakeHeard) wakeHeard.classList.add('hidden');
            if (!wakeWanted) setWakeStatus('');
            else if (session) setWakeStatus('Paused during the call', 'on');
        }
    }

    if (wakeToggle && !DESKTOP_SHELL) {
        if (!WW || !WW.supported) {
            wakeToggle.disabled = true;
            if (wakePhraseInput) wakePhraseInput.disabled = true;
            if (wakeSensitivity) wakeSensitivity.disabled = true;
            setWakeStatus('Not supported in this browser (try Chrome or Edge).', 'err');
        }
        wakeToggle.addEventListener('change', () => {
            wakeWanted = wakeToggle.checked;
            localStorage.setItem('ais_wake_on', wakeWanted ? '1' : '0');
            if (!wakeWanted) {
                if (wake) wake.stop();
                if (wakeHeard) wakeHeard.classList.add('hidden');
            }
            syncWake();
        });
    }

    if (wakePhraseInput) {
        const commitPhrase = () => {
            const value = wakePhraseInput.value.trim();
            if (value) {
                localStorage.setItem(phraseKey(language), value);
            } else {
                localStorage.removeItem(phraseKey(language));   // back to the default
            }
            renderWakePhrase();
            if (wake) wake.setPhrase(wakePhraseFor(language));
        };
        wakePhraseInput.addEventListener('change', commitPhrase);
        wakePhraseInput.addEventListener('blur', commitPhrase);
        wakePhraseInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); wakePhraseInput.blur(); }
        });
    }

    if (wakeSensitivity) {
        wakeSensitivity.value = currentSensitivity();
        wakeSensitivity.addEventListener('change', () => {
            localStorage.setItem('ais_wake_sens', wakeSensitivity.value);
            if (wake) wake.setSensitivity(wakeSensitivity.value);
        });
    }

    /* ---------------- confirmation for destructive actions ---------------- */

    const confirmOverlay = document.getElementById('confirmOverlay');
    const confirmIcon = document.getElementById('confirmIcon');
    const confirmTitle = document.getElementById('confirmTitle');
    const confirmBody = document.getElementById('confirmBody');
    const confirmPath = document.getElementById('confirmPath');
    const confirmYes = document.getElementById('confirmYes');
    const confirmNo = document.getElementById('confirmNo');

    let pendingConfirm = null;

    const CONFIRM_COPY = {
        delete_item: (a) => ({
            icon: '🗑️',
            title: 'Move to Recycle Bin?',
            body: 'Gemini wants to delete "' + (a.name || '?') + '" from your ' +
                  (a.location || 'desktop') + '.',
            note: 'It goes to the Recycle Bin, so you can restore it.'
        }),
        rename_item: (a) => ({
            icon: '✏️',
            title: 'Rename?',
            body: 'Rename "' + (a.name || '?') + '" to "' + (a.new_name || '?') + '" in your ' +
                  (a.location || 'desktop') + '.',
            note: ''
        }),
        move_item: (a) => ({
            icon: '📦',
            title: 'Move?',
            body: 'Move "' + (a.name || '?') + '" from ' + (a.location || 'desktop') +
                  ' into ' + (a.to_location || a.location || 'desktop') +
                  (a.to_path ? '/' + a.to_path : '') + '.',
            note: ''
        }),
        schedule_routine: (a) => ({
            icon: '🔁',
            title: 'Set up this routine?',
            body: (a.name || 'Routine') + '\nWhen: ' + (a.schedule || '?'),
            note: String(a.prompt || '').slice(0, 160) +
                  ' — it can read and look things up, never delete or send.'
        }),
        send_email: (a) => ({
            icon: '✉️',
            title: 'Send this email?',
            body: 'To: ' + (a.to || '?') + '\nSubject: ' + (a.subject || '(none)'),
            note: String(a.body || '').slice(0, 160) +
                  (String(a.body || '').length > 160 ? '…' : '')
        })
    };

    // Queued, not overwritten. The model can emit two destructive tool calls in
    // ONE toolCall message; _runTool awaits both under a Promise.all. If the
    // second prompt replaced the first, the first promise would never settle,
    // Promise.all would never resolve, no toolResponse would ever be sent and
    // the conversation would hang forever.
    const confirmQueue = [];

    function showConfirm(job) {
        const copy = (CONFIRM_COPY[job.tool] || (() => ({
            icon: '⚠️', title: 'Confirm action',
            body: job.tool + ' ' + JSON.stringify(job.args), note: ''
        })))(job.args || {});

        confirmIcon.textContent = copy.icon;
        confirmTitle.textContent = copy.title;
        confirmBody.textContent = copy.body;
        confirmPath.textContent = copy.note +
            (confirmQueue.length ? `  (+${confirmQueue.length} more to review)` : '');
        confirmOverlay.classList.remove('hidden');
        confirmYes.focus();

        pendingConfirm = (answer) => {
            pendingConfirm = null;
            job.resolve(answer);
            const next = confirmQueue.shift();
            if (next) {
                showConfirm(next);
            } else {
                confirmOverlay.classList.add('hidden');
            }
        };
    }

    function askConfirm(tool, args, resolve) {
        const job = { tool: tool, args: args, resolve: resolve };
        if (pendingConfirm) confirmQueue.push(job);
        else showConfirm(job);
    }

    /**
     * Voice mode for the confirm box: she asks out loud and you answer out
     * loud, so the buttons become a fallback rather than the main path.
     */
    function setConfirmVoiceMode(question) {
        confirmIcon.textContent = '🎙️';
        confirmTitle.textContent = 'Waiting for your answer';
        confirmBody.textContent = 'She is asking whether to ' + question + '.';
        confirmPath.textContent = 'Say yes or no — or use the buttons.';
        confirmOverlay.classList.remove('hidden');
        confirmOverlay.classList.add('listening');
    }

    /** Deny everything outstanding — e.g. the call ended while a prompt was up. */
    function cancelPendingConfirms() {
        while (confirmQueue.length) confirmQueue.shift().resolve(false);
        if (pendingConfirm) {
            const done = pendingConfirm;
            pendingConfirm = null;
            done(false);
        }
        confirmOverlay.classList.add('hidden');
        confirmOverlay.classList.remove('listening');
    }

    confirmYes.addEventListener('click', () => pendingConfirm && pendingConfirm(true));
    confirmNo.addEventListener('click', () => pendingConfirm && pendingConfirm(false));
    confirmOverlay.addEventListener('click', (e) => {
        // Click-outside cancels; it must never count as approval.
        if (e.target === confirmOverlay && pendingConfirm) pendingConfirm(false);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && pendingConfirm) pendingConfirm(false);
    });

    /* ---------------- actions & timers ---------------- */

    const ACTION_LABEL = {
        create_folder: 'Created folder',
        list_folder: 'Listed folder',
        write_note: 'Saved note',
        delete_item: 'Moved to Recycle Bin',
        rename_item: 'Renamed',
        move_item: 'Moved',
        set_timer: 'Timer set',
        list_timers: 'Checked timers',
        cancel_timer: 'Timer cancelled',
        add_event: 'Added to calendar',
        list_events: 'Checked calendar',
        delete_event: 'Removed from calendar',
        get_weather: 'Checked weather',
        file_info: 'Checked file details',
        memory: 'Updated memory',
        session_search: 'Searched past conversations',
        skill_view: 'Opened skill',
        skill_manage: 'Updated skill',
        schedule_routine: 'Routine set',
        list_routines: 'Checked routines',
        cancel_routine: 'Routine removed',
        list_emails: 'Checked email',
        read_email: 'Opened email',
        search_emails: 'Searched email',
        send_email: 'Sent email',
        email_status: 'Checked Outlook',
        timer_done: 'Timer finished'
    };

    /**
     * Show every real action in the transcript, pass or fail.
     *
     * This is the user's independent check on the agent: if it says it made a
     * folder and no green row appeared, it did not make a folder.
     */
    function showAction(name, args, result) {
        const empty = transcriptEl.querySelector('.transcript-empty');
        if (empty) empty.remove();

        const ok = !!(result && result.ok);
        const row = document.createElement('div');
        row.className = 'action-row ' + (ok ? 'ok' : 'fail');

        const icon = document.createElement('span');
        icon.className = 'action-icon';
        icon.textContent = ok ? '✓' : '✕';

        const body = document.createElement('span');
        body.className = 'action-body';
        const label = ACTION_LABEL[name] || name;
        if (ok) {
            body.textContent = label + (args && args.name ? ' — ' + args.name : '');
            if (result.path) {
                const p = document.createElement('span');
                p.className = 'action-path';
                p.textContent = result.path;
                body.appendChild(p);
            }
        } else if (result && result.opened_compose) {
            // Not sent, not a failure either: the email is open and waiting.
            row.className = 'action-row pending';
            icon.textContent = '✉';
            body.textContent = 'Email ready in ' + (result.where || 'your mail app') +
                ' — press Send there (not sent yet)';
        } else if (result && result.declined) {
            row.className = 'action-row declined';
            icon.textContent = '⊘';
            body.textContent = label + ' — you cancelled this';
        } else {
            body.textContent = label + ' failed — ' + ((result && result.error) || 'unknown error');
        }

        row.append(icon, body);
        transcriptEl.appendChild(row);
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
        breakTranscript();
    }

    const timers = [];

    function startTimer(seconds, label) {
        const ms = Math.max(1, Math.round(seconds)) * 1000;
        const text = label || 'Timer';

        showAction('set_timer', { name: text + ' · ' + formatDuration(seconds) }, { ok: true });

        const id = setTimeout(() => {
            // Three rising notes, clearly different from the wake chime.
            playChime([{ hz: 880 }, { hz: 1108 }, { hz: 1320, dur: 0.3 }]);
            showAction('timer_done', { name: text }, { ok: true });
            notify('⏰ ' + text, 'Your timer has finished.');
        }, ms);
        timers.push(id);
    }

    function formatDuration(s) {
        s = Math.round(s);
        if (s < 60) return s + 's';
        const m = Math.floor(s / 60);
        const rem = s % 60;
        return rem ? m + 'm ' + rem + 's' : m + 'm';
    }

    function notify(title, body) {
        if (!('Notification' in window)) return;
        if (Notification.permission === 'granted') {
            new Notification(title, { body: body });
        } else if (Notification.permission !== 'denied') {
            Notification.requestPermission().then((p) => {
                if (p === 'granted') new Notification(title, { body: body });
            });
        }
    }

    window.addEventListener('beforeunload', () => timers.forEach(clearTimeout));

    /* ---------------- dismissal ("goodbye" -> standby) ---------------- */

    // Two independent paths end a call, and that redundancy is deliberate:
    //   1. this local matcher — instant, no round trip, catches the obvious
    //      sign-offs from the built-in list below;
    //   2. Gemini calling end_conversation — slower, but understands intent,
    //      so it catches phrasings no list would ever cover.
    // Nothing here is user-configurable: you should not have to type your own
    // goodbyes for the thing to hang up.
    function sleepPhrasesFor(lang) {
        return WW ? (WW.DEFAULT_SLEEP[lang] || WW.DEFAULT_SLEEP.en) : 'goodbye, bye';
    }

    // Transcription arrives in fragments, so accumulate the current utterance
    // and test that. Reset whenever the turn ends.
    let userUtterance = '';
    let dismissing = false;
    let pendingFarewell = null;   // runs once she finishes saying goodbye

    function resetUtterance() { userUtterance = ''; }

    function checkForDismissal(fragment) {
        if (!WW || dismissing || !session) return;
        userUtterance = (userUtterance + ' ' + fragment).trim().slice(-300);

        // Test only the final clause. SMART transcription punctuates, so
        // "Thanks for the help. Goodbye." still ends the call while the
        // length guard keeps a long sentence that merely mentions it alive.
        const clauses = userUtterance.split(/[.!?;؟׃]+/).filter((c) => c.trim());
        const target = clauses.length ? clauses[clauses.length - 1] : userUtterance;

        // Anchored to the end and held to a high bar: ending someone's call by
        // mistake is far worse than missing one "goodbye".
        const { score, phrase } = WW.scoreAny(target, sleepPhrasesFor(language), true);
        if (score < WW.THRESHOLDS.strict) return;

        dismissing = true;
        console.info('Dismissal heard:', JSON.stringify(userUtterance), '~', phrase, score.toFixed(2));
        markTranscript(LANGS[language].ui.dismissed);
        setStatus('idle', LANGS[language].ui.dismissed);

        // Cut immediately — the point of a sign-off is that it stops talking.
        endSession().then(() => { dismissing = false; resetUtterance(); });
    }

    /* ---------------- history recording ---------------- */

    function openHistoryRecord() {
        // A language swap reopens the socket mid-conversation; keep appending
        // to the record that is already open rather than splitting it in two.
        if (histId) return;
        sessionTurns = [];
        sessionEmotions = [];
        sessionStart = Date.now();
        histId = window.AISHistory
            ? window.AISHistory.startVoiceSession(language)
            : null;
    }

    async function closeHistoryRecord() {
        if (!histId || !window.AISHistory) return;

        const id = histId;
        const turns = sessionTurns.filter((t) => t.text.trim());
        const emotions = sessionEmotions.slice();
        const durationMs = sessionStart ? Date.now() - sessionStart : 0;
        const dominant = window.AISHistory.dominantEmotion(emotions);

        histId = null;

        // Nothing was actually said — don't leave an empty row behind.
        if (!turns.length && !emotions.length) {
            window.AISHistory.remove(id);
            return;
        }

        window.AISHistory.update(id, {
            turns: turns,
            emotions: emotions,
            durationMs: durationMs,
            summaryState: turns.length ? 'pending' : 'none'
        });

        if (!turns.length) return;

        let summary = '';
        try {
            const res = await fetch('/api/summarize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    turns: turns,
                    language: language,
                    dominant_emotion: dominant
                })
            });
            const data = await res.json();
            summary = (data && data.summary) || '';
            window.AISHistory.update(id, {
                summary: summary,
                summaryState: summary ? 'done' : 'none'
            });
        } catch (_) {
            window.AISHistory.update(id, { summaryState: 'failed' });
        }

        // The record lives in the database; saving it there also makes it
        // searchable for session_search (unless Memory is switched off).
    }

    /* ---------------- session lifecycle ---------------- */

    const handlers = {
        onStatus: (state, message) => {
            // Don't let the outgoing session's "Session ended." flash over the
            // "switching language…" message while we reconnect.
            if (swapping && state === 'idle') return;
            setStatus(state, message);
        },
        onEmotion: (args) => {
            renderEmotion(args);
            sessionEmotions.push({
                emotion: (args.emotion || 'neutral').toLowerCase(),
                confidence: typeof args.confidence === 'number' ? args.confidence : 0,
                energy: args.energy || '',
                evidence: args.evidence || '',
                at: Date.now()
            });
            saveProgress();
        },
        // Belt and braces: the wake marker is sent as text (which produces no
        // input transcription), but never let it reach the transcript.
        onUserText: (t) => {
            const clean = t.split('__WAKE__').join('').trim();
            if (!clean) return;
            appendTranscript('user', clean);
            checkForDismissal(clean);
        },
        onAgentText: (t) => appendTranscript('agent', t),
        onTurnComplete: () => {
            breakTranscript();
            resetUtterance();
            // She has finished the farewell — now drop to standby.
            if (pendingFarewell) { const f = pendingFarewell; pendingFarewell = null; f(); }
        },
        onInterrupted: () => { breakTranscript(); resetUtterance(); },

        // Gemini judged that the user signed off. This is the semantic path;
        // the local phrase matcher is the fast one. Either may fire first.
        // Let her actually finish saying goodbye. Tearing the socket down the
        // instant the tool fires cuts the farewell off mid-word.
        onEndRequested: () => {
            if (dismissing || !session) return;
            dismissing = true;
            setStatus('live', LANGS[language].ui.signingOff);

            let done = false;
            const finish = () => {
                if (done) return;
                done = true;
                clearTimeout(guard);
                markTranscript(LANGS[language].ui.dismissed);
                setStatus('idle', LANGS[language].ui.dismissed);
                endSession().then(() => { dismissing = false; resetUtterance(); });
            };
            pendingFarewell = finish;
            // Safety net: if she never finishes the turn, go to standby anyway.
            const guard = setTimeout(finish, 7000);
        },

        onAction: (name, args, result) => showAction(name, args, result),
        onTimer: (seconds, label) => startTimer(seconds, label),
        onConfirm: (name, args, resolve) => askConfirm(name, args, resolve),

        // She is about to ask out loud; the box is a read-along, not a gate.
        onConfirmPending: (name, args, question) => setConfirmVoiceMode(question),
        onConfirmResolved: () => cancelPendingConfirms(),
        onMicLevel: (level) => {
            // Cheap VU meter on the ring.
            const scale = 1 + Math.min(level * 2.2, 0.5);
            talkBtn.style.setProperty('--level', scale.toFixed(3));
        }
    };

    async function startSession(opts) {
        opts = opts || {};
        talkBtn.disabled = true;
        syncWake();                 // release the mic from the wake listener
        openHistoryRecord();
        try {
            session = new LiveSession(handlers, {
                language: language,
                greet: !!opts.greet
            });
            await session.start();
            muteBtn.disabled = false;
            endBtn.disabled = false;
        } catch (err) {
            setStatus('error', err.message || String(err));
            if (session) { await session.stop(); session = null; }
            await closeHistoryRecord();
            syncWake();             // failed to start — resume listening
        } finally {
            talkBtn.disabled = !voiceEnabled;
        }
    }

    async function endSession() {
        if (session) { await session.stop(); session = null; }
        muteBtn.disabled = true;
        endBtn.disabled = true;
        muteBtn.classList.remove('active');
        muteBtn.innerHTML = '<i class="fas fa-microphone-slash"></i> Mute';
        talkBtn.style.removeProperty('--level');
        breakTranscript();
        resetUtterance();
        // A confirm box left open after the call ends can never be answered
        // into a live session, so deny it rather than stranding the promise.
        cancelPendingConfirms();

        // A language swap continues into a new session, so keep one record.
        if (!swapping) {
            if (wakeWanted) cueEnd();      // audible "call over, back to standby"
            closeHistoryRecord();          // fire and forget: summary lands later
            renderLanguage();              // restore the idle greeting
            syncWake();                    // resume hands-free listening
        }
    }

    talkBtn.addEventListener('click', () => {
        if (session) { endSession(); } else { startSession(); }
    });

    endBtn.addEventListener('click', endSession);

    muteBtn.addEventListener('click', () => {
        if (!session) return;
        const next = !session.muted;
        session.setMuted(next);
        muteBtn.classList.toggle('active', next);
        muteBtn.innerHTML = next
            ? '<i class="fas fa-microphone"></i> Unmute'
            : '<i class="fas fa-microphone-slash"></i> Mute';
    });

    // Let the desktop shell start a call the same way the wake word does in
    // the browser build — greeting included — without faking a button click.
    window.AISStartVoiceSession = (opts) => startSession(opts || {});
    window.AISEndVoiceSession = () => endSession();

    /**
     * Tools settings changed. Switching something OFF is enforced by the
     * server straight away, but switching something ON only reaches her
     * through the setup message — which is sent once, at connect. So a live
     * call is reopened, the same way a language change is. The on-screen
     * transcript and the history record carry over.
     */
    window.AISToolsChanged = async () => {
        if (!session || swapping) return;
        swapping = true;
        setStatus('connecting', 'Tools updated — reconnecting…');
        markTranscript('🧰 Tools updated');
        try {
            await endSession();
            await startSession();
        } finally {
            swapping = false;
        }
    };

    // Don't leave the mic hot if the tab goes away.
    window.addEventListener('beforeunload', () => {
        if (session) session.stop();
        if (wake) wake.stop();
    });

    renderLanguage();

    // Restore the hands-free preference (browser build only).
    if (!DESKTOP_SHELL && wakeToggle && !wakeToggle.disabled
        && localStorage.getItem('ais_wake_on') === '1') {
        wakeToggle.checked = true;
        wakeWanted = true;
        syncWake();
    }
});
