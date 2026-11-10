// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Run Halide's HelloWasm app (apps/HelloWasm/app/core.cpp, as built by buildlog
// for wasm32-emscripten) under Node, headless, and print the frame it renders.
//
//   node scripts/hellowasm_node.cjs DIR/index.js FRAMES
//
// The page's DOM is replaced by the least the app and SDL2's Emscripten port
// touch: a canvas whose WebGL context is unavailable (so SDL falls back to its
// software renderer, which presents each frame with putImageData on a 2D
// context), the "threads" slider (value 1) and the "runtime" text element. The
// main loop's requestAnimationFrame is a setImmediate. After FRAMES frames the
// script prints, as JSON, the frame count and the SHA-256 of the last frame's
// pixels in Halide's layout (one little-endian ARGB word per pixel, rows from
// the top), which is what reaction_diffusion_render writes, then exits.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const crypto = require("crypto");

const [jsPath, framesArg] = process.argv.slice(2);
const FRAMES = Number(framesArg || 32);
const W = 1024, H = 1024;

let presented = 0;
let lastPixels = null;
let done = false;

function finish(code, extra) {
  if (done) return;
  done = true;
  const out = { frames: presented, ...extra };
  if (lastPixels) {
    // Canvas RGBA bytes back to Halide's ARGB words (little endian: B, G, R, A).
    const argb = Buffer.alloc(W * H * 4);
    for (let i = 0; i < W * H; i++) {
      argb[4 * i + 0] = lastPixels[4 * i + 2];
      argb[4 * i + 1] = lastPixels[4 * i + 1];
      argb[4 * i + 2] = lastPixels[4 * i + 0];
      argb[4 * i + 3] = lastPixels[4 * i + 3];
    }
    out.sha256 = crypto.createHash("sha256").update(argb).digest("hex");
    if (process.env.HELLOWASM_DUMP) fs.writeFileSync(process.env.HELLOWASM_DUMP, argb);
  }
  process.stdout.write(JSON.stringify(out) + "\n");
  process.exit(code);
}

function element(props) {
  return Object.assign(
    {
      style: {},
      addEventListener() {},
      removeEventListener() {},
      getBoundingClientRect: () => ({ left: 0, top: 0, width: W, height: H, x: 0, y: 0 }),
      setAttribute() {},
      getAttribute() { return null; },
    },
    props
  );
}

const ctx2d = {
  createImageData: (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
  getImageData: (x, y, w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
  putImageData(img) {
    presented++;
    lastPixels = Uint8Array.from(img.data);
    if (presented >= FRAMES) finish(0);
  },
  drawImage() {},
  fillRect() {},
  clearRect() {},
};

const canvas = element({
  id: "canvas",
  width: W,
  height: H,
  clientWidth: W,
  clientHeight: H,
  getContext: (kind) => (kind === "2d" ? ctx2d : null),
  requestPointerLock() {},
});
const threads = element({ id: "threads", value: process.env.HELLOWASM_THREADS || "1" });
const runtime = element({ id: "runtime", innerHTML: "" });
const byId = { canvas, threads, runtime };

const document = element({
  getElementById: (id) => byId[id] || null,
  querySelector: (sel) => (sel === "#canvas" || sel === "canvas" ? canvas : byId[sel.replace(/^#/, "")] || null),
  createElement: () => element({}),
  body: element({}),
  documentElement: element({}),
  fullscreenElement: null,
  pointerLockElement: null,
  hidden: false,
  visibilityState: "visible",
  title: "",
});

Object.assign(globalThis, {
  document,
  screen: { width: W, height: H, availWidth: W, availHeight: H },
  requestAnimationFrame: (f) => setImmediate(() => f(performance.now())),
  cancelAnimationFrame() {},
  addEventListener() {},
  removeEventListener() {},
  devicePixelRatio: 1,
  innerWidth: W,
  innerHeight: H,
});
globalThis.window = globalThis;
globalThis.WebGLRenderingContext = function () {};
globalThis.WebGL2RenderingContext = function () {};
Object.defineProperty(globalThis, "navigator", {
  value: { userAgent: "node", language: "en-US", languages: ["en-US"], getGamepads: () => [] },
  configurable: true,
});

globalThis.Module = {
  canvas,
  print: (s) => process.stderr.write(`[app] ${s}\n`),
  printErr: (s) => process.stderr.write(`[app] ${s}\n`),
  locateFile: (p) => path.join(path.dirname(path.resolve(jsPath)), p),
  onAbort: (what) => finish(2, { error: `abort: ${what}` }),
};

process.on("uncaughtException", (e) => finish(3, { error: String(e && e.stack || e) }));
setTimeout(() => finish(4, { error: "timeout" }), Number(process.env.HELLOWASM_TIMEOUT_MS || 120000));

const src = fs.readFileSync(jsPath, "utf8");
globalThis.require = require;
globalThis.__filename = path.resolve(jsPath);
globalThis.__dirname = path.dirname(path.resolve(jsPath));
vm.runInThisContext(src, { filename: jsPath });
