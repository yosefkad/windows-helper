document.addEventListener('DOMContentLoaded', () => {
    const inputText = document.getElementById('inputText');
    const charCount = document.querySelector('.char-count');
    const btnSmartAnalyze = document.getElementById('btnSmartAnalyze');
    const loadingDiv = document.getElementById('loading');
    const resultContainer = document.getElementById('resultContainer');
    const resultText = document.getElementById('resultText');
    const aiBrain = document.getElementById('aiBrain');

    // Level 3 Controls
    const toneSelector = document.getElementById('toneSelector');
    const micBtn = document.getElementById('micBtn');

    // Char counter
    inputText.addEventListener('input', () => {
        charCount.textContent = `${inputText.value.length} תווים`;
    });

    // Level 3: Speech to Text
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SpeechRec) {
        const recognition = new SpeechRec();
        recognition.continuous = false;
        recognition.interimResults = false;
        recognition.maxAlternatives = 3;

        // The recognition language MUST match what the user is actually
        // speaking — a Hebrew sentence decoded as en-US comes back as
        // nonsense. Read the language switch at click time so it always
        // reflects the current selection.
        function currentRecognitionLang() {
            const langs = window.LIVE_LANGUAGES || {};
            const code = localStorage.getItem('ais_voice_lang');
            if (langs[code] && langs[code].bcp47) return langs[code].bcp47;
            // Fall back to the page/browser language rather than assuming English.
            return document.documentElement.lang === 'he'
                ? 'he-IL'
                : (navigator.language || 'en-US');
        }

        micBtn.addEventListener('click', () => {
            if (micBtn.classList.contains('recording')) {
                recognition.stop();
                return;
            }
            recognition.lang = currentRecognitionLang();
            try {
                recognition.start();
            } catch (e) {
                // start() throws if a previous session has not fully ended.
                console.warn('Speech recognition busy:', e);
            }
        });

        recognition.onstart = () => {
            micBtn.classList.add('recording');
            micBtn.title = 'Listening (' + recognition.lang + ') — click to stop';
        };

        recognition.onend = () => {
            micBtn.classList.remove('recording');
            micBtn.title = 'Speak';
        };

        recognition.onerror = (e) => {
            micBtn.classList.remove('recording');
            if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
                alert('Microphone permission was denied.');
            } else if (e.error === 'no-speech') {
                micBtn.title = 'Did not catch that — try again';
            }
        };

        recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            inputText.value += (inputText.value ? ' ' : '') + transcript;
            inputText.dispatchEvent(new Event('input')); // Trigger char count
        };
    } else {
        micBtn.style.display = 'none'; // Hide if not supported
    }

    async function analyzeText() {
        const text = inputText.value.trim();
        const targetTone = toneSelector.value;

        if (!text) {
            alert('אנא הזן טקסט לניתוח');
            return;
        }

        // UI Reset
        resultContainer.classList.add('hidden');
        if (aiBrain) aiBrain.classList.add('hidden');
        loadingDiv.classList.remove('hidden');
        btnSmartAnalyze.disabled = true;

        try {
            const response = await fetch('/api/analyze', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    text: text,
                    target_tone: targetTone // Send Level 3 param
                })
            });

            const data = await response.json();

            if (data.error) {
                alert('An error occurred: ' + data.error);
            } else {
                // Populate Results
                // API returns { result: "...", meta: { ... } }
                if (data.meta && data.meta.detected_type) {
                    const meta = data.meta;
                    let resultHtml = `
                <div class="result-box">
                    <div class="ai-brain">
                        <div class="brain-item"><span class="label">Detected Type:</span> <span class="value ${meta.detected_type}">${meta.detected_type.toUpperCase()}</span></div>
                        <div class="brain-item"><span class="label">Confidence:</span> <span class="value">${meta.confidence}</span></div>
                        <div class="brain-item"><span class="label">Action Taken:</span> <span class="value action">${meta.action_taken}</span></div>
                    </div>
                    <div class="result-text">${data.result.replace(/\n/g, '<br>')}</div>
                </div>
            `;
                    resultText.innerHTML = resultHtml;

                    // Save to History
                    addToHistory(text, meta.detected_type, meta.action_taken);
                    resultContainer.classList.remove('hidden'); // Make sure result container is visible
                    resultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' }); // Scroll to result
                } else {
                    resultText.textContent = "Error: Could not analyze text (Invalid response format).";
                    resultContainer.classList.remove('hidden');
                }
            }

        } catch (error) {
            resultText.textContent = "Error: " + error.message;
            resultContainer.classList.remove('hidden');
        } finally {
            // Reset button state
            loadingDiv.classList.add('hidden');
            btnSmartAnalyze.disabled = false;
            btnSmartAnalyze.innerHTML = originalBtnText;
        }
    }

    btnSmartAnalyze.addEventListener('click', () => analyzeText());
});

// Level 2: Feedback System
async function sendFeedback(type) {
    const text = document.getElementById("inputText").value;
    const detectedTypeElement = document.querySelector(".ai-brain .brain-item .value");
    const detectedType = detectedTypeElement ? detectedTypeElement.classList[1] : 'unknown'; // get class name

    try {
        await fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: text,
                detected_type: detectedType,
                feedback: type,
                timestamp: new Date().toISOString()
            })
        });
        alert("Thanks for your feedback! We will use this to improve the AI.");
    } catch (e) {
        console.error("Feedback failed", e);
    }
}

// Level 2: History System
// Now owned by history.js, which records BOTH text analyses and voice sessions.
// It exposes a compatible window.addToHistory(text, type, action), so the call
// in analyzeText() above keeps working unchanged.

function copyToClipboard() {
    const text = document.querySelector(".result-text").innerText;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.querySelector('.copy-btn');
        const originalText = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-check"></i> הועתק';
        setTimeout(() => {
            btn.innerHTML = originalText;
        }, 2000);
    });
}
