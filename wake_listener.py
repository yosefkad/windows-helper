"""
Offline wake-word listener for the desktop app.

Why this exists: the browser wake word uses the Web Speech API, which WebView2
ships but does not actually run — SpeechRecognition constructs and then never
fires a start event. So in the desktop shell the hotword has to be detected in
Python instead.

Vosk does it fully offline with no API key. Every word of the phrase must be
in the model's vocabulary — see _vocabulary_report().

Why it used to wake up on its own: the grammar held ONLY the wake phrase and
its words, so the decoder had to map every sound — a TV, chatter, a door —
onto "hey", "gemini" or [unk], and "gemini" alone counted as a trigger. It also
fired on partial (mid-sound) guesses. Measured on synthetic clips, 13 of 26
ordinary sentences ("hey jimmy", "Germany is a big country", TV news) and 2 of
8 noise clips woke it. Now:
  * the grammar also offers ~150 decoy words (near-misses like jimmy, jenny,
    germany, genie, plus everyday words), so other sounds have somewhere else
    to go;
  * only FINAL results count, never partials;
  * the WHOLE phrase must be heard, in order — the distinctive word with
    confidence >= MIN_CONF, fillers like "hey" >= MIN_CONF_FILLER — spoken
    within a human length of time. "gemini" on its own no longer wakes it.
"""
import json
import queue
import threading
import time

try:
    import sounddevice as sd
    import vosk
    # Keep vosk's own warnings (notably "Ignoring word missing in vocabulary")
    # out of stdout, but we now detect that case ourselves before arming.
    vosk.SetLogLevel(-1)
    AVAILABLE = True
    IMPORT_ERROR = None
except Exception as e:                       # pragma: no cover
    AVAILABLE = False
    IMPORT_ERROR = str(e)

SAMPLE_RATE = 16000
BLOCK = 4000

# UI language -> vosk model language tag.
LANG_MODELS = {"en": "en-us", "he": "he"}

# Vosk word confidence needed. The distinctive word ("gemini") must be sure;
# a short filler like "hey" is often clipped or mumbled, so it gets a lower bar.
MIN_CONF = 0.80
MIN_CONF_FILLER = 0.50
# The phrase has to be said within this many seconds (a real "hey gemini" is
# ~0.5-1.2 s); longer spans are noise the decoder stretched into words.
MAX_PHRASE_S = 2.5

# Decoys: somewhere for everything that is NOT the wake phrase to land.
# Near-misses matter most — without "jimmy" in the grammar, "hey jimmy" can
# only come out as "hey gemini".
DECOYS = """
hey hi hello jimmy jenny jeremy jamie gemma germany german genie gym jim gem
mini many money honey any many minnie jiminy chemistry general gentle jelly
geneva jenna jennifer dominic johnny journey jersey tiny skinny benny kenny
the a an and or but is are was were be been it its to of in on at for from by
with about as that this these those what who how why where when which there
here now then so well just like know think get got go going gone come came
see look say said tell told want need make take give put let okay ok yes yeah
no not do does did have has had will would can could should may might must
i me my you your he him his she her we us our they them their one two three
four five six seven eight nine ten time day night morning today tomorrow
news weather people man woman thing things good bad right left up down out
off over again back more some all very really much little big small new old
buy milk home work food water play stop turn open close call phone thanks
thank please sorry wait hold on minute second next first last
""".split()


def _fold(s):
    """Cheap normalisation so 'hey, Gemini!' == 'hey gemini'."""
    return " ".join("".join(c for c in (s or "").lower()
                            if c.isalnum() or c.isspace()).split())


class WakeListener:
    def __init__(self, phrase="hey gemini", lang="en-us", on_wake=None):
        self.phrase = phrase
        self.lang = lang
        self.on_wake = on_wake

        self._thread = None
        self._stream_open = threading.Event()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._q = queue.Queue()

        self.model = None
        self.model_lang = None
        self.error = None
        self.last_heard = ""
        self.triggered_at = 0.0
        self.last_score = None
        self.running = False

    # -- model ----------------------------------------------------------

    def load(self):
        if not AVAILABLE:
            self.error = f"vosk/sounddevice unavailable: {IMPORT_ERROR}"
            return False
        if self.model is not None and self.model_lang == self.lang:
            return True
        try:
            # vosk calls sys.exit() when a language has no model, which raises
            # SystemExit — not caught by `except Exception`.
            self.model = vosk.Model(lang=self.lang)
            self.model_lang = self.lang
            return True
        except SystemExit:
            self.model = None
            self.error = (f"No offline speech model for '{self.lang}'. "
                          f"Hands-free needs one; use an English wake phrase, "
                          f"or install a {self.lang} Vosk model.")
            return False
        except Exception as e:
            self.model = None
            self.error = f"Could not load the '{self.lang}' speech model: {e}"
            return False

    def _vocabulary_report(self):
        """
        Which words of the phrase the model cannot possibly recognise.

        This is the difference between "wake word does not work" and knowing
        why. A grammar built from out-of-vocabulary words is accepted silently
        by KaldiRecognizer, arms cleanly, reports running=True — and can never
        match. Hebrew against the en-us model is exactly this case:
        find_word('גמיני') == -1.
        """
        words = [w for w in _fold(self.phrase).split() if w]
        missing = []
        for w in words:
            try:
                if self.model.vosk_model_find_word(w) < 0:
                    missing.append(w)
            except Exception:
                pass          # can't check — assume present rather than block
        return words, missing

    # -- lifecycle ------------------------------------------------------

    def start(self):
        if self.running:
            return True
        if not self.load():
            return False

        words, missing = self._vocabulary_report()
        if not words:
            self.error = "The wake phrase is empty."
            return False
        if missing:
            self.error = (
                f"The offline model for '{self.lang}' does not know "
                f"{', '.join(repr(m) for m in missing)}, so this phrase could "
                f"never be heard. Pick a wake phrase made of ordinary "
                f"{'Hebrew' if self.lang.startswith('he') else 'English'} words."
            )
            return False

        # Join any previous worker BEFORE clearing the stop flag. Clearing it
        # while an old thread is still inside its loop resurrects that thread:
        # two workers then share one queue, each recogniser gets half the audio
        # and neither ever hears a whole phrase — with the mic held twice.
        self._join_worker()
        self._drain()

        self.error = None
        self._stop.clear()
        self._paused.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        # Wait for the stream to actually open so start() does not report
        # success for a worker that dies immediately.
        if not self._stream_open.wait(timeout=5):
            self._stop.set()
            if self.error is None:
                self.error = "Could not open the microphone for the wake word."
            return False
        return True

    def _join_worker(self):
        t = self._thread
        if t and t.is_alive():
            self._stop.set()
            t.join(timeout=3)
        self._thread = None

    def _drain(self):
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def stop(self):
        """Synchronous: returns once the mic is actually released."""
        self._join_worker()
        self.running = False
        self._stream_open.clear()
        self._drain()

    def pause(self):
        """Release the mic to the Live session for the duration of a call."""
        self._paused.set()
        self._drain()

    def resume(self):
        self._drain()
        self._paused.clear()

    def set_phrase(self, phrase):
        if not phrase or phrase == self.phrase:
            return
        self.phrase = phrase
        # The grammar is baked into the recogniser at construction, so a live
        # listener has to be rebuilt or it keeps listening for the old phrase.
        if self.running:
            self.stop()
            self.start()

    def set_lang(self, lang):
        target = LANG_MODELS.get(lang, lang)
        if target == self.lang:
            return
        was_running = self.running
        if was_running:
            self.stop()
        self.lang = target
        self.model = None
        if was_running:
            self.start()

    # -- worker ---------------------------------------------------------

    def _callback(self, indata, frames, time_info, status):
        if not self._paused.is_set():
            self._q.put(bytes(indata))

    def _grammar(self):
        # ensure_ascii=False matters: with the default, a non-Latin phrase is
        # handed to vosk as the literal text "\u05d4\u05d9\u05d9" rather than
        # as the word, so the grammar silently contains nothing but [unk].
        phrase = _fold(self.phrase)
        mine = set(phrase.split())
        decoys = []
        for w in dict.fromkeys(DECOYS):
            if w in mine:
                continue
            try:
                if self.model is not None and self.model.vosk_model_find_word(w) < 0:
                    continue          # a decoy the model doesn't know is just noise
            except Exception:
                pass
            decoys.append(w)
        return json.dumps([phrase] + decoys + ["[unk]"], ensure_ascii=False)

    def _decide(self, payload, final):
        """
        The wake phrase, or None. Only a FINAL result with word timings counts:
        the whole phrase, in order, confident enough (see MIN_CONF), said
        within MAX_PHRASE_S. Anything less — one word, a shaky guess, a partial — is
        not someone calling her.
        """
        if not final:
            return None
        words = payload.get("result") or []
        target = _fold(self.phrase).split()
        n = len(target)
        if not n or len(words) < n:
            return None
        for i in range(len(words) - n + 1):
            seg = words[i:i + n]
            if [_fold(w.get("word")) for w in seg] != target:
                continue
            confs = [float(w.get("conf", 0)) for w in seg]
            span = float(seg[-1].get("end", 0)) - float(seg[0].get("start", 0))
            self.last_score = {"confs": [round(c, 2) for c in confs], "span_s": round(span, 2)}
            key = max(range(n), key=lambda k: len(target[k]))      # the distinctive word
            sure = confs[key] >= MIN_CONF and min(confs) >= MIN_CONF_FILLER
            if sure and 0 < span <= MAX_PHRASE_S:
                return " ".join(w.get("word") for w in seg)
        return None

    def _run(self):
        try:
            rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE, self._grammar())
            rec.SetWords(True)            # word confidences + timings for _decide
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK,
                                   dtype="int16", channels=1,
                                   callback=self._callback):
                self.running = True
                self._stream_open.set()
                while not self._stop.is_set():
                    try:
                        data = self._q.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    if self._paused.is_set():
                        continue

                    final = rec.AcceptWaveform(data)
                    payload = json.loads(rec.Result() if final else rec.PartialResult())
                    text = payload.get("text") or payload.get("partial") or ""
                    if not text:
                        continue
                    # Only surface what sounds like the phrase. Everything else
                    # now lands on a decoy word ("bad", "news"…), which would
                    # only confuse the "heard:" readout.
                    mine = set(_fold(self.phrase).split())
                    if mine & set(_fold(text).split()):
                        self.last_heard = text

                    if self._decide(payload, final):
                        # Debounce: one trigger per few seconds.
                        if time.time() - self.triggered_at > 3:
                            self.triggered_at = time.time()
                            rec.Reset()
                            if self.on_wake:
                                try:
                                    self.on_wake(text)
                                except Exception:
                                    pass
        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False
            self._stream_open.set()   # unblock start() even on immediate death
