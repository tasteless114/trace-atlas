'use strict';
/*
 * Heat pixel colouring runs on the GPU-less worker threads: the per-pixel ramp lookup is
 * the only O(pixels) step of drawing, so it is split into horizontal bands and handed to
 * one Worker per core. The main thread keeps compositing, which is cheap.
 */
let ramp = null;
let littleEndian = true;

(() => {
  const probe = new Uint32Array([1]);
  littleEndian = new Uint8Array(probe.buffer)[0] === 1;
})();

function colorize(buffer) {
  const data = new Uint8ClampedArray(buffer);
  const pixels = new Uint32Array(buffer);
  const size = pixels.length;
  if (littleEndian) {
    for (let i = 0; i < size; i++) {
      const alpha = data[i * 4 + 3];
      if (!alpha) continue;
      let index = (alpha * 1.42 + 0.5) | 0;
      if (index > 255) index = 255;
      const at = index * 4;
      let outAlpha = (alpha * 1.45 + 0.5) | 0;
      if (outAlpha > 238) outAlpha = 238;
      pixels[i] = ramp[at] | (ramp[at + 1] << 8) | (ramp[at + 2] << 16) | (outAlpha << 24);
    }
  } else {
    for (let i = 0; i < size; i++) {
      const at = i * 4;
      const alpha = data[at + 3];
      if (!alpha) continue;
      let index = (alpha * 1.42 + 0.5) | 0;
      if (index > 255) index = 255;
      const from = index * 4;
      let outAlpha = (alpha * 1.45 + 0.5) | 0;
      data[at] = ramp[from];
      data[at + 1] = ramp[from + 1];
      data[at + 2] = ramp[from + 2];
      data[at + 3] = outAlpha > 238 ? 238 : outAlpha;
    }
  }
  return buffer;
}

self.onmessage = event => {
  const message = event.data;
  if (message.ramp) ramp = new Uint8ClampedArray(message.ramp);
  if (!ramp) return;
  const buffer = colorize(message.buffer);
  self.postMessage({id: message.id, buffer}, [buffer]);
};
