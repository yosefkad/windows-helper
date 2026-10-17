/**
 * Wake-word listener ("Hey Gemini" / "היי ג'מיני" / anything you type).
 *
 * Built on the Web Speech API (SpeechRecognition). It is not a true always-on
 * keyword spotter — it streams to the browser's recognition service — but it
 * needs no extra key or model download. (Picovoice Porcupine is the on-device
 * upgrade path if you ever want real offline hotwording.)
 *
 * Matching is deliberately FUZZY. Recognisers mangle rare words constantly:
 * "gemini" comes back as jiminy / jeremy / germany / gem in he. Requiring an
 * exact string match is why naive wake words feel broken. Instead we phonetic-
 * fold both sides and score them with edit distance, so near-misses still fire.
 *
 * Mic handoff matters: this listener and the Gemini Live session must never
 * hold the microphone at the same time. The caller stops this listener the
 * moment it fires, and restarts it when the live session ends.
 */
(function () {
    'use strict';

    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;

    const DEFAULT_PHRASE = { en: 'hey gemini', he: 'היי גמיני' };

    // Phrases that recognisers handle well, offered as presets.
    const PRESETS = {
        en: ['hey gemini', 'computer', 'hey smart', 'hey assistant', 'okay computer', 'listen up'],
        he: ['היי גמיני', 'מחשב', 'שלום מחשב', 'היי עוזר', 'תקשיב לי']
    };

    // Dismissal phrases: saying one of these ends the call and drops back to
    // standby, so the agent stops answering until the wake word is said again.
    const DEFAULT_SLEEP = {
        en: 'goodbye, bye, stop listening, that is all, thanks bye, go to sleep',
        he: 'להתראות, ביי, זהו, תודה ביי, מספיק, לך לישון'
    };

    const BCP47 = { en: 'en-US', he: 'he-IL' };

    // Sensitivity -> similarity threshold. Lower = fires more easily.
    //
    // Calibrated against a corpus of real recogniser output: genuine
    // mis-hearings of "hey gemini" bottom out around 0.71 ("hey jimmy",
    // "hey jeremy") while unrelated speech tops out around 0.57 ("I need to
    // send an email"). Anything in that gap separates them cleanly, so
    // `normal` sits at 0.68 and `loose` keeps extra headroom for noise.
    const THRESHOLDS = { loose: 0.60, normal: 0.68, strict: 0.80 };
    const DEFAULT_SENSITIVITY = 'loose';

    /* ------------------------------------------------------------------ *
     * text normalisation
     * ------------------------------------------------------------------ */

    const HEBREW_FINALS = { 'ם': 'מ', 'ן': 'נ', 'ץ': 'צ', 'ך': 'כ', 'ף': 'פ' };

    function normalize(s) {
        return (s || '')
            .toLowerCase()
            .replace(/[֑-ׇ]/g, '')            // niqqud / cantillation
            .replace(/[׳״'"`´’‘]/g, '')                 // geresh, gershayim, quotes
            .replace(/[םןץךף]/g, (c) => HEBREW_FINALS[c] || c)
            .replace(/[.,!?;:\-–—_/\\]/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    /**
     * Collapse sounds that recognisers swap for each other, so "gemini" and
     * "jiminy" land on the same string. Latin only — Hebrew passes through.
     */
    function phoneticFold(s) {
        return normalize(s)
            .replace(/ph/g, 'f')
            .replace(/ck|q|c/g, 'k')
            .replace(/x/g, 'ks')
            .replace(/[jg]/g, 'j')       // soft g == j
            .replace(/z/g, 's')
            .replace(/[yiea]/g, 'i')     // front vowels collapse
            .replace(/[ou]/g, 'o')       // back vowels collapse
            .replace(/h/g, '')           // frequently dropped
            .replace(/(.)\1+/g, '$1')    // doubles
            .replace(/\s+/g, ' ')
            .trim();
    }

    function levenshtein(a, b) {
        if (a === b) return 0;
        if (!a.length) return b.length;
        if (!b.length) return a.length;

        let prev = new Array(b.length + 1);
        let cur = new Array(b.length + 1);
        for (let j = 0; j <= b.length; j++) prev[j] = j;

        for (let i = 1; i <= a.length; i++) {
            cur[0] = i;
            for (let j = 1; j <= b.length; j++) {
                const cost = a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1;
                cur[j] = Math.min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost);
            }
            const t = prev; prev = cur; cur = t;
        }
        return prev[b.length];
    }

    function similarity(a, b) {
        const max = Math.max(a.length, b.length);
        if (!max) return 0;
        return 1 - levenshtein(a, b) / max;
    }

    /**
     * Best match score for `phrase` anywhere in `heard`.
     * Returns { score, matched } where score is 0..1.
     */
    function scorePhrase(heard, phrase) {
        const h = phoneticFold(heard);
        const p = phoneticFold(phrase);
        if (!h || !p) return { score: 0, matched: '' };

        // 1. Run-together hit, e.g. "heygemini" — recognisers do this a lot.
        const hTight = h.replace(/ /g, '');
        const pTight = p.replace(/ /g, '');
        if (pTight && hTight.indexOf(pTight) !== -1) return { score: 1, matched: phrase };

        const hTokens = h.split(' ').filter(Boolean);
        const pTokens = p.split(' ').filter(Boolean);
        if (!hTokens.length) return { score: 0, matched: '' };

        let best = 0;
        let bestText = '';

        // 2. Sliding window over the heard text, allowing the recogniser to
        //    have merged or split a word (n-1, n, n+1).
        for (const size of [pTokens.length - 1, pTokens.length, pTokens.length + 1]) {
            if (size < 1) continue;
            for (let i = 0; i + size <= hTokens.length; i++) {
                const window = hTokens.slice(i, i + size).join('');
                const s = similarity(window, pTight);
                if (s > best) { best = s; bestText = hTokens.slice(i, i + size).join(' '); }
            }
        }

        // 3. The distinctive word on its own ("gemini" without the "hey").
        //    Held to a slightly higher bar since it is a single token.
        const keyword = pTokens.slice().sort((a, b) => b.length - a.length)[0];
        if (keyword && keyword.length >= 4) {
            for (const t of hTokens) {
                const s = similarity(t, keyword);
                if (s > best) { best = s; bestText = t; }
            }
        }

        return { score: best, matched: bestText };
    }

    /**
     * Like scorePhrase, but only looks at the END of what was said.
     *
     * Dismissals need this: "ok thanks, goodbye" should end the call, while
     * "I said goodbye to my friend yesterday" must not. Anchoring to the tail
     * separates the two, because a real sign-off is the last thing you say.
     */
    const MAX_EXTRA_TOKENS = 3;

    function scoreTail(heard, phrase) {
        const hTokens = phoneticFold(heard).split(' ').filter(Boolean);
        const pTokens = phoneticFold(phrase).split(' ').filter(Boolean);
        if (!hTokens.length || !pTokens.length) return 0;

        // A real sign-off is a SHORT utterance. Without this, any sentence that
        // merely contains the word ends the call — "my dog goes to sleep at
        // nine", "how do you say goodbye in French". Window matching alone
        // cannot tell those apart, because the word really is in there.
        if (hTokens.length > pTokens.length + MAX_EXTRA_TOKENS) return 0;

        const tail = hTokens.slice(-(pTokens.length + 1)).join(' ');
        return scorePhrase(tail, phrase).score;
    }

    /**
     * Best score across a comma-separated list of phrases.
     * Returns { score, phrase }.
     */
    function scoreAny(heard, phraseList, atEnd) {
        const phrases = String(phraseList || '')
            .split(',')
            .map((p) => p.trim())
            .filter(Boolean);
        let best = 0;
        let bestPhrase = '';
        for (const p of phrases) {
            const s = atEnd ? scoreTail(heard, p) : scorePhrase(heard, p).score;
            if (s > best) { best = s; bestPhrase = p; }
        }
        return { score: best, phrase: bestPhrase };
    }

    /* ------------------------------------------------------------------ *
     * listener
     * ------------------------------------------------------------------ */

    class WakeWordListener {
        constructor(opts) {
            opts = opts || {};
            this.lang = opts.lang || 'en';
            this.phrase = opts.phrase || DEFAULT_PHRASE[this.lang] || DEFAULT_PHRASE.en;
            this.threshold = opts.threshold || THRESHOLDS[DEFAULT_SENSITIVITY];

            this.onWake = opts.onWake || function () {};
            this.onStatus = opts.onStatus || function () {};
            this.onHeard = opts.onHeard || function () {};

            this.rec = null;
            this.wantRunning = false;   // user intent
            this.running = false;       // recogniser is live
            this.starting = false;      // spin-up in flight (guards a race)
            this.fatal = null;
            this.cooldownUntil = 0;

            this._restartTimer = null;
            this._restartCount = 0;
            this._restartWindowStart = 0;
        }

        static get supported() { return !!SR; }

        setLang(lang, phrase) {
            const changed = lang !== this.lang;
            this.lang = lang;
            if (phrase) this.phrase = phrase;
            if (changed && this.wantRunning) {
                this._teardown();
                this._spinUp();
            }
        }

        setPhrase(phrase) {
            this.phrase = phrase || DEFAULT_PHRASE[this.lang] || DEFAULT_PHRASE.en;
        }

        setSensitivity(name) {
            this.threshold = THRESHOLDS[name] || THRESHOLDS[DEFAULT_SENSITIVITY];
        }

        start() {
            if (!SR) {
                this.onStatus('unsupported', 'This browser has no SpeechRecognition.');
                return false;
            }
            if (this.fatal) {
                this.onStatus('error', this.fatal);
                return false;
            }
            this.wantRunning = true;
            this._spinUp();
            return true;
        }

        _spinUp() {
            if (!this.wantRunning || this.running || this.starting) return;
            clearTimeout(this._restartTimer);
            this.starting = true;

            const rec = new SR();
            this.rec = rec;
            rec.continuous = true;
            rec.interimResults = true;   // interim = much faster trigger
            rec.maxAlternatives = 5;     // more chances to land a match
            rec.lang = BCP47[this.lang] || 'en-US';

            rec.onstart = () => {
                this.starting = false;
                this.running = true;
                this.onStatus('listening', 'Waiting for the wake word…');
            };

            rec.onresult = (event) => {
                if (Date.now() < this.cooldownUntil) return;

                let bestScore = 0;
                let bestHeard = '';

                // Chrome keeps appending to one growing list, so only look at
                // what arrived since the last event.
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    const result = event.results[i];
                    for (let a = 0; a < result.length; a++) {
                        const heard = result[a].transcript;
                        if (!heard || !heard.trim()) continue;
                        // The phrase field accepts a comma-separated list.
                        const { score } = scoreAny(heard, this.phrase, false);
                        if (score > bestScore) { bestScore = score; bestHeard = heard.trim(); }
                    }
                }

                if (bestHeard) this.onHeard(bestHeard, bestScore, this.threshold);

                if (bestScore >= this.threshold) {
                    this.cooldownUntil = Date.now() + 3000;
                    this.onStatus('woke', bestHeard);
                    this.onWake(bestHeard, bestScore);
                }
            };

            rec.onerror = (e) => {
                const err = e.error;
                if (err === 'not-allowed' || err === 'service-not-allowed') {
                    this.fatal = 'Microphone permission denied for wake word.';
                    this.wantRunning = false;
                    this.onStatus('error', this.fatal);
                } else if (err === 'audio-capture') {
                    this.fatal = 'No microphone found.';
                    this.wantRunning = false;
                    this.onStatus('error', this.fatal);
                }
                // 'no-speech' / 'aborted' / 'network' are routine; onend restarts.
            };

            rec.onend = () => {
                this.starting = false;
                this.running = false;
                if (!this.wantRunning) {
                    this.onStatus('idle', 'Wake word off.');
                    return;
                }
                // Chrome ends the session every few seconds of silence. Each gap
                // is time we are deaf, so come back almost immediately — with a
                // storm guard in case the service is refusing outright.
                const now = Date.now();
                if (now - this._restartWindowStart > 10000) {
                    this._restartWindowStart = now;
                    this._restartCount = 0;
                }
                this._restartCount++;
                const delay = this._restartCount > 12 ? 2000 : 50;

                clearTimeout(this._restartTimer);
                this._restartTimer = setTimeout(() => this._spinUp(), delay);
            };

            try {
                rec.start();
            } catch (_) {
                // Throws if the previous instance has not fully released yet.
                this.starting = false;
                clearTimeout(this._restartTimer);
                this._restartTimer = setTimeout(() => this._spinUp(), 250);
            }
        }

        _teardown() {
            clearTimeout(this._restartTimer);
            if (this.rec) {
                this.rec.onend = null;
                this.rec.onresult = null;
                this.rec.onerror = null;
                this.rec.onstart = null;
                try { this.rec.abort(); } catch (_) { /* noop */ }
                this.rec = null;
            }
            this.running = false;
            this.starting = false;
        }

        stop(opts) {
            opts = opts || {};
            if (!opts.keepIntent) this.wantRunning = false;
            this._teardown();
            if (!opts.keepIntent) this.onStatus('idle', 'Wake word off.');
        }
    }

    WakeWordListener.DEFAULT_PHRASE = DEFAULT_PHRASE;
    WakeWordListener.PRESETS = PRESETS;
    WakeWordListener.BCP47 = BCP47;
    WakeWordListener.THRESHOLDS = THRESHOLDS;
    WakeWordListener.DEFAULT_SENSITIVITY = DEFAULT_SENSITIVITY;
    WakeWordListener.normalize = normalize;
    WakeWordListener.phoneticFold = phoneticFold;
    WakeWordListener.similarity = similarity;
    WakeWordListener.scorePhrase = scorePhrase;
    WakeWordListener.scoreTail = scoreTail;
    WakeWordListener.scoreAny = scoreAny;
    WakeWordListener.DEFAULT_SLEEP = DEFAULT_SLEEP;

    window.WakeWordListener = WakeWordListener;
})();
