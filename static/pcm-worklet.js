/**
 * PCM capture worklet.
 * Runs on the audio thread, accumulates mic samples into ~100ms chunks
 * (the chunk size the Live API docs recommend) and posts them to the main
 * thread as Float32. Conversion to 16-bit PCM happens in live.js.
 */
class PCMCaptureProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        const chunkMs = (options && options.processorOptions && options.processorOptions.chunkMs) || 100;
        this.chunkSize = Math.round((sampleRate * chunkMs) / 1000);
        this.buffer = new Float32Array(this.chunkSize);
        this.offset = 0;
        this.muted = false;
        this.port.onmessage = (e) => {
            if (e.data && e.data.type === 'mute') this.muted = !!e.data.value;
        };
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || input.length === 0) return true;
        const channel = input[0];
        if (!channel) return true;

        for (let i = 0; i < channel.length; i++) {
            this.buffer[this.offset++] = this.muted ? 0 : channel[i];
            if (this.offset === this.chunkSize) {
                // Copy out: the underlying buffer gets reused immediately.
                this.port.postMessage(this.buffer.slice(0));
                this.offset = 0;
            }
        }
        return true;
    }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
