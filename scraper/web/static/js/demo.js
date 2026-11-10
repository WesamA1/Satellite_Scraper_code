// Demo mode: draws the trained attribute model deciding one image, attribute
// by attribute, as a network lighting up. Something to show people, not a
// tool -- it only reads /api/demo/sample and saves nothing.
//
// What is real: the picture, the strip of cells (the image's embedding, pooled),
// every line from a cell to an answer (the head's weight times the embedding:
// how hard that cell pushes toward or away from the answer), the percentages,
// the answer picked and the annotator's answer. What is a drawing: the frozen
// image model in the middle, which is far too big to show neuron by neuron.
//
// The whole picture is a function of one clock, so pausing, changing speed and
// starting over are all just changes to that number.

(() => {
  const W = 1280;
  const H = 720;
  const INTRO = 4.3; // seconds before the first attribute: picture -> image model -> numbers
  const PER_ATTRIBUTE = 2.8;
  const DECIDED_AT = 1.9; // seconds into an attribute when its answer is picked
  const OUTRO = 4;

  // Light on dark in both themes, like the log console.
  const BG = '#0d0c08';
  const CARD = '#16140e';
  const INK = [232, 227, 211];
  const SOFT = [179, 173, 148];
  const FAINT = [140, 133, 108];
  const WARM = [255, 122, 66]; // pushes toward an answer
  const COOL = [79, 179, 201]; // pushes away from it
  const OK = [127, 176, 106];
  const MISS = [232, 121, 90];
  const DISPLAY = '"Barlow Condensed", "Arial Narrow", sans-serif';
  const UI = '"Barlow", -apple-system, "Segoe UI", sans-serif';
  const MONO = '"Space Mono", ui-monospace, Menlo, Consolas, monospace';

  const IMG = { x: 40, y: 104, size: 260, patches: 14 };
  const NET = { xs: [372, 438, 504, 570], counts: [7, 9, 9, 7], cy: 234, gap: 29 };
  const STRIP = { x: 664, y: 104, w: 20, h: 540 };
  const OUT = { x: 900, top: 186, bottom: 556, bar: 236 };
  const LOG = { x: 40, y: 452, colWidth: 290 };

  const canvas = document.getElementById('demo-canvas');
  const stage = document.getElementById('demo-stage');
  const controls = document.getElementById('demo-controls');
  const toggleBtn = document.getElementById('demo-toggle');
  const nextBtn = document.getElementById('demo-next');
  const speedSelect = document.getElementById('demo-speed');
  const fullscreenBtn = document.getElementById('demo-fullscreen');
  const nowLine = document.getElementById('demo-now');
  const emptyBox = document.getElementById('demo-empty');
  const emptyText = document.getElementById('demo-empty-text');
  const emptyLink = document.getElementById('demo-empty-link');
  const retryBtn = document.getElementById('demo-retry');
  const ctx = canvas.getContext('2d');

  let scene = null; // what is on screen
  let upcoming = null; // the next image, loaded ahead of time
  let fetching = false;
  let clock = 0;
  let playing = true;
  let speed = 1;
  let lastFrame = null;
  let announced = -1;
  let generation = 0; // bumped whenever the image is changed by hand, so a slow load can't land late

  // ---- Small helpers ----

  const clamp = (v) => Math.max(0, Math.min(1, v));
  const span = (t, from, to) => clamp((t - from) / (to - from));
  const ease = (v) => v * v * (3 - 2 * v);
  const frac = (v) => v - Math.floor(v);
  const rgba = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${clamp(a)})`;
  const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  const sameSet = (a, b) => a.length === b.length && a.every((v) => b.includes(v));

  // A fixed "random" number for slot `i` of this image: the drawing of the
  // image model is the same every time the same image comes round.
  const noise = (i) => frac(Math.sin(i * 127.1 + scene.seed * 311.7) * 43758.5453);

  function fit(str, max) {
    if (ctx.measureText(str).width <= max) return str;
    let cut = str;
    while (cut.length > 1 && ctx.measureText(`${cut}…`).width > max) cut = cut.slice(0, -1);
    return `${cut.trimEnd()}…`;
  }

  function text(str, x, y, { font, color = INK, alpha = 1, align = 'left', max = null } = {}) {
    if (alpha <= 0.01) return 0;
    ctx.font = font;
    ctx.textAlign = align;
    ctx.fillStyle = rgba(color, alpha);
    const shown = max ? fit(str, max) : str;
    ctx.fillText(shown, x, y);
    return ctx.measureText(shown).width;
  }

  // A check or a cross, drawn rather than typed so it looks the same in every font.
  function mark(x, y, ok, alpha) {
    if (alpha <= 0.01) return;
    ctx.strokeStyle = rgba(ok ? OK : MISS, alpha);
    ctx.lineWidth = 2.4;
    ctx.lineCap = 'round';
    ctx.beginPath();
    if (ok) {
      ctx.moveTo(x, y + 6);
      ctx.lineTo(x + 4.5, y + 10.5);
      ctx.lineTo(x + 13, y);
    } else {
      ctx.moveTo(x + 1, y);
      ctx.lineTo(x + 11, y + 10);
      ctx.moveTo(x + 11, y);
      ctx.lineTo(x + 1, y + 10);
    }
    ctx.stroke();
  }

  function dot(x, y, r, color, alpha, glow = 0) {
    if (alpha <= 0.01) return;
    ctx.shadowColor = rgba(color, alpha);
    ctx.shadowBlur = glow;
    ctx.fillStyle = rgba(color, alpha);
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  // A point `u` of the way along the curve from a cell to an answer.
  function along(from, to, u) {
    const bend = (to.x - from.x) * 0.5;
    const v = 1 - u;
    const x = v * v * v * from.x + 3 * v * v * u * (from.x + bend) + 3 * v * u * u * (to.x - bend) + u * u * u * to.x;
    const y = v * v * v * from.y + 3 * v * v * u * from.y + 3 * v * u * u * to.y + u * u * u * to.y;
    return { x, y };
  }

  const netNode = (col, i) => ({ x: NET.xs[col], y: NET.cy + (i - (NET.counts[col] - 1) / 2) * NET.gap });

  // ---- Loading ----

  function loadImage(id) {
    return new Promise((resolve) => {
      const img = new Image();
      let triedFile = false;
      img.onload = () => resolve(img);
      img.onerror = () => {
        if (triedFile) return resolve(null);
        triedFile = true;
        img.src = `/api/images/${id}/file`;
      };
      img.src = `/api/images/${id}/thumb?size=640`;
    });
  }

  function pickedText(attribute) {
    return attribute.picked.length ? attribute.picked.map((k) => attribute.options[k]).join(', ') : 'None of these';
  }

  function truthText(attribute) {
    const said = [...attribute.truth.answers.map((k) => attribute.options[k]), ...attribute.truth.other];
    return said.length ? said.join(', ') : 'None of these';
  }

  function build(sample, image) {
    const count = sample.cells.length;
    const cellH = STRIP.h / count;
    const attributes = sample.attributes.map((attribute) => {
      const n = attribute.options.length;
      const gap = Math.min(64, (OUT.bottom - OUT.top) / Math.max(1, n - 1));
      const first = (OUT.top + OUT.bottom) / 2 - (gap * (n - 1)) / 2;
      const strongest = Math.max(1e-9, ...attribute.pushes.flat().map(Math.abs));
      const perAnswer = n <= 4 ? 7 : n <= 6 ? 5 : 4; // the strongest few cells per answer; the rest would be a blur
      const links = [];
      attribute.pushes.forEach((row, k) => {
        row
          .map((push, cell) => ({ cell, push }))
          .sort((a, b) => Math.abs(b.push) - Math.abs(a.push))
          .slice(0, perAnswer)
          .forEach(({ cell, push }) => links.push({ k, cell, push, strength: Math.abs(push) / strongest }));
      });
      return {
        ...attribute,
        ys: attribute.options.map((_, k) => first + k * gap),
        links,
        sources: new Set(links.map((link) => link.cell)),
        match: attribute.truth ? sameSet(attribute.picked, attribute.truth.answers) && !attribute.truth.other.length : null,
      };
    });
    return {
      sample,
      image,
      attributes,
      seed: sample.image.id,
      cellH,
      cellY: (i) => STRIP.y + (i + 0.5) * cellH,
      cellMax: Math.max(1e-9, ...sample.cells.map(Math.abs)),
      total: INTRO + attributes.length * PER_ATTRIBUTE + OUTRO,
    };
  }

  async function fetchScene(after) {
    const resp = await fetch(after == null ? '/api/demo/sample' : `/api/demo/sample?after=${after}`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      const error = new Error(body.detail || `The demo couldn't load (error ${resp.status}).`);
      error.status = resp.status;
      throw error;
    }
    const sample = await resp.json();
    return build(sample, await loadImage(sample.image.id));
  }

  function show(next) {
    scene = next;
    upcoming = null;
    clock = 0;
    announced = -1;
    nowLine.textContent = '';
  }

  function showProblem(error) {
    scene = null;
    stage.hidden = true;
    controls.hidden = true;
    emptyBox.hidden = false;
    emptyText.textContent = error.message;
    emptyLink.hidden = ![404, 409].includes(error.status); // no trained model, or nothing embedded: both are fixed on Train model
  }

  // The first image, or one asked for by hand: the stage waits for it.
  async function start() {
    const mine = ++generation;
    const after = scene ? scene.sample.image.id : null;
    emptyBox.hidden = true;
    stage.hidden = false;
    controls.hidden = false;
    scene = null;
    upcoming = null;
    try {
      const next = await fetchScene(after);
      if (mine === generation) show(next);
    } catch (error) {
      if (mine === generation) showProblem(error);
    }
  }

  // The next image, fetched while the current one is finishing so the change is seamless.
  async function preload() {
    const mine = generation;
    fetching = true;
    try {
      const next = await fetchScene(scene.sample.image.id);
      if (mine === generation) upcoming = next;
    } catch (error) {
      if (mine === generation && scene) upcoming = scene; // show this one again, and ask again after it
    } finally {
      fetching = false;
    }
  }

  function nextImage() {
    if (upcoming && upcoming !== scene) show(upcoming);
    else start();
  }

  // ---- Drawing ----

  function drawHeader(index, local) {
    text('GROUND STATION', 40, 50, { font: `700 24px ${DISPLAY}` });
    text('ATTRIBUTE MODEL · DEMO', 200, 50, { font: `400 12px ${MONO}`, color: FAINT });
    // One tick per attribute: green or red once decided when an annotator's answer exists to compare with.
    const n = scene.attributes.length;
    for (let i = 0; i < n; i++) {
      const x = 1240 - (n - i) * 18;
      const decided = i < index || (i === index && local >= DECIDED_AT);
      let color = rgba(FAINT, 0.35);
      if (decided) {
        const match = scene.attributes[i].match;
        color = rgba(match === null ? INK : match ? OK : MISS, 0.95);
      } else if (i === index) {
        color = rgba(WARM, 0.55 + 0.45 * Math.sin(clock * 6));
      }
      ctx.fillStyle = color;
      ctx.fillRect(x, 40, 14, 5);
    }
  }

  function drawImage() {
    const shown = ease(span(clock, 0, 0.6));
    const { x, y, size, patches } = IMG;
    text('IMAGE', x, 92, { font: `400 12px ${MONO}`, color: FAINT });
    ctx.fillStyle = CARD;
    ctx.fillRect(x, y, size, size);
    if (scene.image) {
      const scale = Math.min(size / scene.image.naturalWidth, size / scene.image.naturalHeight);
      const w = scene.image.naturalWidth * scale;
      const h = scene.image.naturalHeight * scale;
      ctx.globalAlpha = shown;
      ctx.drawImage(scene.image, x + (size - w) / 2, y + (size - h) / 2, w, h);
      ctx.globalAlpha = 1;
    } else {
      text('picture unavailable', x + size / 2, y + size / 2, { font: `400 12px ${MONO}`, color: FAINT, align: 'center' });
    }

    // Cut into patches, which light up one by one and stream into the image model.
    const cell = size / patches;
    const scanned = span(clock, 0.45, 0.8);
    ctx.strokeStyle = rgba(INK, 0.13 * scanned);
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < patches; i++) {
      ctx.moveTo(x + i * cell, y);
      ctx.lineTo(x + i * cell, y + size);
      ctx.moveTo(x, y + i * cell);
      ctx.lineTo(x + size, y + i * cell);
    }
    ctx.stroke();
    for (let p = 0; p < patches * patches; p++) {
      const at = 0.5 + p * 0.0065;
      if (clock < at || clock > at + 0.7) continue;
      const px = x + (p % patches) * cell;
      const py = y + Math.floor(p / patches) * cell;
      ctx.fillStyle = rgba(WARM, 0.55 * (1 - span(clock, at, at + 0.45)));
      ctx.fillRect(px, py, cell, cell);
      if (p % 3 === 0) {
        const u = ease(span(clock, at, at + 0.6));
        const to = netNode(0, p % NET.counts[0]);
        dot(px + cell / 2 + (to.x - px - cell / 2) * u, py + cell / 2 + (to.y - py - cell / 2) * u, 2, WARM, Math.sin(Math.PI * u), 6);
      }
    }
    ctx.strokeStyle = rgba(SOFT, 0.5);
    ctx.strokeRect(x + 0.5, y + 0.5, size - 1, size - 1);

    text(scene.sample.image.family || `Image ${scene.sample.image.id}`, x, 394, {
      font: `700 24px ${DISPLAY}`,
      alpha: shown,
      max: size,
    });
    text(scene.sample.held_out ? 'HELD OUT · NEVER TRAINED ON' : 'NOT IN THE TRAINING SET', x, 414, {
      font: `400 11px ${MONO}`,
      color: scene.sample.held_out ? OK : FAINT,
      alpha: shown,
    });
  }

  function drawImageModel() {
    text('IMAGE MODEL · FROZEN', NET.xs[0] - 14, 92, { font: `400 12px ${MONO}`, color: FAINT });
    const on = NET.xs.map((_, col) => ease(span(clock, 1.3 + col * 0.35, 1.75 + col * 0.35)));

    for (let col = 0; col < NET.xs.length - 1; col++) {
      const live = Math.min(on[col], on[col + 1]);
      ctx.strokeStyle = rgba(SOFT, 0.04 + 0.1 * live);
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      for (let i = 0; i < NET.counts[col]; i++) {
        for (let j = 0; j < NET.counts[col + 1]; j++) {
          const a = netNode(col, i);
          const b = netNode(col + 1, j);
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
        }
      }
      ctx.stroke();
      // A handful of signals keep travelling between the layers.
      for (let i = 0; i < NET.counts[col]; i++) {
        for (let j = 0; j < NET.counts[col + 1]; j++) {
          const slot = col * 100 + i * 10 + j;
          if (noise(slot) > 0.16) continue;
          const u = frac(clock * 0.55 + noise(slot + 500));
          const a = netNode(col, i);
          const b = netNode(col + 1, j);
          dot(a.x + (b.x - a.x) * u, a.y + (b.y - a.y) * u, 1.6, WARM, live * Math.sin(Math.PI * u) * 0.9, 5);
        }
      }
    }
    for (let col = 0; col < NET.xs.length; col++) {
      for (let i = 0; i < NET.counts[col]; i++) {
        const { x, y } = netNode(col, i);
        const level = (0.25 + 0.75 * noise(col * 31 + i)) * (0.8 + 0.2 * Math.sin(clock * 2.2 + i * 1.7 + col));
        dot(x, y, 6.5, [38, 34, 25], 1);
        dot(x, y, 6.5, WARM, on[col] * level, 10 * on[col] * level);
        ctx.strokeStyle = rgba(SOFT, 0.35 + 0.4 * on[col]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(x, y, 6.5, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    text(scene.sample.model.label, NET.xs[0] - 14, 394, { font: `700 20px ${DISPLAY}`, color: SOFT });
    text('DRAWN SIMPLIFIED · THE REAL ONE IS FAR BIGGER', NET.xs[0] - 14, 414, { font: `400 11px ${MONO}`, color: FAINT });
  }

  function drawStrip(attribute, glow) {
    const cells = scene.sample.cells;
    text(`${scene.sample.numbers} NUMBERS`, STRIP.x + STRIP.w / 2, 92, { font: `400 12px ${MONO}`, color: FAINT, align: 'center' });
    const last = NET.xs.length - 1;
    for (let i = 0; i < cells.length; i++) {
      const y = scene.cellY(i);
      const from = netNode(last, i % NET.counts[last]);
      const filled = ease(span(clock, 2.85 + i * 0.02, 3.1 + i * 0.02));
      ctx.strokeStyle = rgba(SOFT, 0.03 + 0.05 * filled);
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(STRIP.x, y);
      ctx.stroke();
      const u = span(clock, 2.5 + i * 0.02, 2.9 + i * 0.02);
      if (u > 0 && u < 1) dot(from.x + (STRIP.x - from.x) * u, from.y + (y - from.y) * u, 1.8, WARM, Math.sin(Math.PI * u), 6);

      // The cell's colour is its real value: warm above zero, cool below.
      const value = cells[i] / scene.cellMax;
      const colour = mix([29, 26, 19], value >= 0 ? WARM : COOL, filled * Math.pow(Math.abs(value), 0.7));
      ctx.fillStyle = rgba(colour);
      ctx.fillRect(STRIP.x, y - scene.cellH / 2 + 1, STRIP.w, scene.cellH - 2);
      if (attribute && attribute.sources.has(i)) {
        ctx.strokeStyle = rgba(INK, 0.85 * glow);
        ctx.lineWidth = 1.2;
        ctx.strokeRect(STRIP.x - 1.5, y - scene.cellH / 2 - 0.5, STRIP.w + 3, scene.cellH + 1);
      }
    }
  }

  function drawDecision(attribute, index, local) {
    const last = index === scene.attributes.length - 1;
    const appear = ease(span(local, 0, 0.35));
    const leave = last ? 1 : 1 - ease(span(local, PER_ATTRIBUTE - 0.25, PER_ATTRIBUTE));
    const fill = ease(span(local, 0.9, DECIDED_AT));
    const decided = ease(span(local, DECIDED_AT, DECIDED_AT + 0.3));
    const visible = appear * leave;
    const left = OUT.x - 14;

    const rule = attribute.kind === 'single' ? 'PICKS ONE' : 'PICKS ANY THAT APPLY';
    text(`ATTRIBUTE ${index + 1} OF ${scene.attributes.length} · ${rule}`, left, 92, {
      font: `400 12px ${MONO}`,
      color: FAINT,
      alpha: visible,
    });
    let size = 40;
    ctx.font = `700 ${size}px ${DISPLAY}`;
    while (size > 24 && ctx.measureText(attribute.name.toUpperCase()).width > 354) {
      size -= 2;
      ctx.font = `700 ${size}px ${DISPLAY}`;
    }
    text(attribute.name.toUpperCase(), left, 138, { font: `700 ${size}px ${DISPLAY}`, alpha: visible, max: 354 });

    // The lines: each is one cell's real push on one answer. Strongest first.
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.lineCap = 'round';
    attribute.links.forEach((link, slot) => {
      const from = { x: STRIP.x + STRIP.w + 3, y: scene.cellY(link.cell) };
      const to = { x: OUT.x - 15, y: attribute.ys[link.k] };
      const begun = 0.25 + (1 - link.strength) * 0.45;
      const grown = ease(span(local, begun, begun + 0.55));
      if (grown <= 0) return;
      const chosen = attribute.picked.includes(link.k);
      const alpha = (0.16 + 0.7 * link.strength) * visible * (chosen ? 1 : 1 - 0.7 * decided);
      const colour = link.push >= 0 ? WARM : COOL;
      ctx.strokeStyle = rgba(colour, alpha);
      ctx.lineWidth = 0.6 + 3.2 * link.strength;
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      const steps = Math.max(2, Math.round(26 * grown));
      for (let s = 1; s <= steps; s++) {
        const p = along(from, to, (s / steps) * grown);
        ctx.lineTo(p.x, p.y);
      }
      ctx.stroke();
      if (grown >= 1) {
        const u = frac(local * 0.85 + noise(900 + slot));
        const p = along(from, to, u);
        dot(p.x, p.y, 1.6 + 2 * link.strength, colour, alpha * Math.sin(Math.PI * u) * 1.6, 8);
      }
    });
    ctx.restore();

    // One output neuron per answer, filling to its real probability.
    attribute.options.forEach((option, k) => {
      const y = attribute.ys[k];
      const probability = attribute.probabilities[k];
      const chosen = attribute.picked.includes(k);
      const alpha = visible * (chosen ? 1 : 1 - 0.5 * decided);
      dot(OUT.x, y, 12, [38, 34, 25], visible);
      dot(OUT.x, y, 12, WARM, alpha * probability * fill, 18 * probability * fill);
      ctx.strokeStyle = rgba(SOFT, 0.8 * alpha);
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(OUT.x, y, 12, 0, Math.PI * 2);
      ctx.stroke();
      if (chosen && decided > 0) {
        ctx.shadowColor = rgba(WARM, visible);
        ctx.shadowBlur = 16;
        ctx.strokeStyle = rgba(WARM, decided * visible);
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.arc(OUT.x, y, 12 + 6 * decided + 1.2 * Math.sin(clock * 5), 0, Math.PI * 2);
        ctx.stroke();
        ctx.shadowBlur = 0;
      }
      const labelX = OUT.x + 26;
      text(option, labelX, y - 2, { font: `${chosen && decided > 0.5 ? 600 : 500} 14px ${UI}`, alpha, max: OUT.bar });
      ctx.fillStyle = rgba(SOFT, 0.18 * visible);
      ctx.fillRect(labelX, y + 6, OUT.bar, 5);
      ctx.fillStyle = rgba(chosen ? mix(SOFT, WARM, decided) : SOFT, alpha);
      ctx.fillRect(labelX, y + 6, OUT.bar * probability * fill, 5);
      if (attribute.kind === 'multi') {
        // Anything past this mark, half, is picked.
        ctx.fillStyle = rgba(INK, 0.55 * visible);
        ctx.fillRect(labelX + OUT.bar / 2, y + 4, 1, 9);
      }
      text(`${Math.round(probability * fill * 100)}%`, labelX + OUT.bar + 44, y + 12, {
        font: `400 12px ${MONO}`,
        color: chosen ? mix(SOFT, WARM, decided) : SOFT,
        alpha,
        align: 'right',
      });
    });

    text('● PUSHES TOWARD', 716, 676, { font: `400 11px ${MONO}`, color: WARM, alpha: 0.9 * visible });
    text('● PUSHES AWAY', 716, 692, { font: `400 11px ${MONO}`, color: COOL, alpha: 0.9 * visible });

    const verdict = decided * leave;
    text('MODEL', left, 606, { font: `400 12px ${MONO}`, color: FAINT, alpha: verdict });
    text(pickedText(attribute), left, 636, { font: `700 30px ${DISPLAY}`, color: WARM, alpha: verdict, max: 354 });
    if (attribute.truth) {
      text('ANNOTATOR', left, 664, { font: `400 12px ${MONO}`, color: FAINT, alpha: verdict });
      const width = text(truthText(attribute), left, 688, { font: `500 17px ${UI}`, color: SOFT, alpha: verdict, max: 320 });
      mark(left + width + 10, 676, attribute.match, verdict);
    }
  }

  function drawOutro(local) {
    const appear = ease(span(local, 0, 0.5));
    const compared = scene.attributes.filter((attribute) => attribute.match !== null);
    const matched = compared.filter((attribute) => attribute.match).length;
    const x = OUT.x - 14;
    if (compared.length) {
      text(`${matched} / ${compared.length}`, x, 340, { font: `700 130px ${DISPLAY}`, alpha: appear });
      text('ATTRIBUTES MATCHED THE ANNOTATOR', x, 378, { font: `400 12px ${MONO}`, color: SOFT, alpha: appear });
      text('exactly, on an image it never trained on', x, 404, { font: `400 15px ${UI}`, color: FAINT, alpha: appear });
    } else {
      text(`${scene.attributes.length}`, x, 340, { font: `700 130px ${DISPLAY}`, alpha: appear });
      text('ATTRIBUTES DECIDED', x, 378, { font: `400 12px ${MONO}`, color: SOFT, alpha: appear });
      text('no annotator has answered this image yet', x, 404, { font: `400 15px ${UI}`, color: FAINT, alpha: appear });
    }
    text('NEXT IMAGE COMING UP', x, 470, { font: `400 12px ${MONO}`, color: FAINT, alpha: appear * (0.5 + 0.5 * Math.sin(clock * 4)) });
  }

  function drawAnswers(index, local) {
    const n = scene.attributes.length;
    text('ANSWERS SO FAR', LOG.x, LOG.y, { font: `400 12px ${MONO}`, color: FAINT });
    const perColumn = Math.max(1, Math.ceil(n / 2));
    const rowH = Math.min(34, 236 / perColumn);
    scene.attributes.forEach((attribute, i) => {
      let alpha = 0;
      if (i < index) alpha = 1;
      else if (i === index) alpha = ease(span(local, DECIDED_AT, DECIDED_AT + 0.4));
      if (alpha <= 0) return;
      const x = LOG.x + Math.floor(i / perColumn) * LOG.colWidth;
      const y = LOG.y + 24 + (i % perColumn) * rowH;
      text(attribute.name.toUpperCase(), x, y, { font: `400 11px ${MONO}`, color: FAINT, alpha, max: 240 });
      const width = text(pickedText(attribute), x, y + 15, { font: `500 14px ${UI}`, alpha, max: 236 });
      if (attribute.match !== null) mark(x + width + 8, y + 4, attribute.match, alpha);
    });
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(640, Math.round(canvas.getBoundingClientRect().width * dpr));
    if (canvas.width !== width) {
      canvas.width = width;
      canvas.height = Math.round((width * H) / W);
    }
    ctx.setTransform(canvas.width / W, 0, 0, canvas.width / W, 0, 0);
    ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = BG;
    ctx.fillRect(0, 0, W, H);

    if (!scene) {
      text('LOADING…', W / 2, H / 2, { font: `400 13px ${MONO}`, color: FAINT, align: 'center' });
      return;
    }
    const n = scene.attributes.length;
    const index = clock < INTRO ? -1 : Math.min(n, Math.floor((clock - INTRO) / PER_ATTRIBUTE));
    const local = index < 0 ? 0 : clock - INTRO - index * PER_ATTRIBUTE;
    const attribute = index >= 0 && index < n ? scene.attributes[index] : null;

    drawHeader(index, local);
    drawImage();
    drawImageModel();
    drawStrip(attribute, attribute ? ease(span(local, 0.2, 0.6)) * (1 - ease(span(local, PER_ATTRIBUTE - 0.25, PER_ATTRIBUTE))) : 0);
    if (attribute) drawDecision(attribute, index, local);
    else if (index === n) drawOutro(local);
    drawAnswers(index, local);

    // Fade in at the start and out at the end, so the change of image is soft.
    const curtain = 1 - ease(span(clock, 0, 0.5)) + ease(span(clock, scene.total - 0.6, scene.total));
    if (curtain > 0) {
      ctx.fillStyle = `rgba(13,12,8,${clamp(curtain)})`;
      ctx.fillRect(0, 0, W, H);
    }
    if (!playing) {
      ctx.fillStyle = 'rgba(13,12,8,0.8)';
      ctx.fillRect(W / 2 - 110, 694, 220, 20);
      text('PAUSED · SPACE TO PLAY', W / 2, 708, { font: `400 12px ${MONO}`, color: SOFT, align: 'center' });
    }
  }

  // Says each decision in words under the controls, for anyone not watching the picture.
  function announce() {
    if (!scene || clock < INTRO) return;
    const n = scene.attributes.length;
    const index = Math.min(n, Math.floor((clock - INTRO) / PER_ATTRIBUTE));
    const local = clock - INTRO - index * PER_ATTRIBUTE;
    const decided = index >= n ? n - 1 : local >= DECIDED_AT ? index : index - 1;
    if (decided === announced || decided < 0) return;
    announced = decided;
    const attribute = scene.attributes[decided];
    const chances = attribute.picked.map((k) => `${Math.round(attribute.probabilities[k] * 100)}%`).join(', ');
    let line = `${scene.sample.image.family || 'Image'} · ${attribute.name}: ${pickedText(attribute)}`;
    if (chances) line += ` (${chances})`;
    if (attribute.truth) line += ` · annotator said ${truthText(attribute)}`;
    nowLine.textContent = line;
  }

  function frame(now) {
    const dt = lastFrame == null ? 0 : Math.min(0.25, (now - lastFrame) / 1000) // a hidden tab resumes where it left off;
    lastFrame = now;
    if (scene) {
      if (playing) clock += dt * speed;
      if (clock > scene.total - OUTRO && !upcoming && !fetching) preload();
      if (clock >= scene.total) {
        if (upcoming) show(upcoming);
        else clock = scene.total; // hold on the last frame until the next image arrives
      }
    }
    announce();
    draw();
    requestAnimationFrame(frame);
  }

  // ---- Controls ----

  function setPlaying(value) {
    playing = value;
    toggleBtn.textContent = playing ? 'Pause' : 'Play';
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (stage.requestFullscreen) stage.requestFullscreen();
  }

  // Where full screen isn't on offer, don't offer it.
  if (!stage.requestFullscreen) {
    fullscreenBtn.hidden = true;
    const keys = document.querySelector('.demo-keys');
    if (keys) keys.textContent = 'Space pauses, N is next image';
  }

  toggleBtn.addEventListener('click', () => setPlaying(!playing));
  canvas.addEventListener('click', () => setPlaying(!playing));
  nextBtn.addEventListener('click', nextImage);
  retryBtn.addEventListener('click', start);
  fullscreenBtn.addEventListener('click', toggleFullscreen);
  speedSelect.addEventListener('change', () => {
    speed = Number(speedSelect.value) || 1;
  });
  document.addEventListener('keydown', (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey || !emptyBox.hidden) return;
    if (['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(event.target.tagName)) return;
    if (event.key === ' ') {
      event.preventDefault();
      setPlaying(!playing);
    } else if (event.key === 'n' || event.key === 'N') nextImage();
    else if (event.key === 'f' || event.key === 'F') toggleFullscreen();
  });

  // Anyone who has asked their computer for less motion gets a still frame and a Play button.
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) setPlaying(false);

  start().then(() => {
    if (!playing && scene) clock = INTRO + DECIDED_AT + 0.4;
  });
  requestAnimationFrame(frame);
})();
