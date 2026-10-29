/*
  vi-render.js — In-place LabVIEW block-diagram renderer for the VI Browser.

  WHAT IT DOES
  ------------
  Renders a VI's block diagram the way the LabVIEW editor shows it: the root
  diagram is painted once, and every Case / Event / Stacked-Sequence structure is
  composited IN PLACE at its real location. Each structure carries a small
  selector ("◀ 1/3 ▶  ‘True’") anchored on its frame, so a reader pages through a
  structure's cases without the diagram jumping — and nested structures page
  inside the case that owns them, recursively. This replaces the older approach
  that stacked every case in separate steppers BELOW the diagram, because that was
  all the HTML export allowed (no positions, no ownership).

  INPUT — the "frames" model (produced by a position-aware renderer such as the
  lvctl `toimages` Convert.vi asset tree). A flat JSON array; each element:

      {
        "Image":     "<base64 PNG>",            // or "Base64 Image" / "ImageFile"
        "Position":  { "Left":int, "Top":int, "Width":int, "Height":int },
        "Cluster":   { "Left":int, "Top":int, "Width":int, "Height":int }, // optional
        "Children":      [int, ...],   // indices into THIS array (sub-frames)
        "Child Indices": [int, ...],   // alias accepted for "Children"
        "Label":     "True"            // optional case/frame selector label
      }

  The tree is encoded by the child-index lists; geometry by Position. Sibling
  frames that share the same Position rectangle are the cases of ONE structure
  (LabVIEW renders every case at the structure's fixed border), so they collapse
  into a single in-place stepper. Coordinates are interpreted relative to the
  owning diagram and auto-detected as absolute-vs-relative per child, so the
  renderer is tolerant of either convention.

  This module is dependency-free and runs same-origin inside the snapshot iframe.
  Public API:  LVRender.render(container, frames, opts) -> { destroy() }
*/
(function (global) {
  'use strict';

  // ── small DOM helpers ───────────────────────────────────────────────────────
  function el(tag, cls, parent) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (parent) parent.appendChild(n);
    return n;
  }
  function imgSrc(frame) {
    const b64 = frame.Image || frame['Base64 Image'] || frame.base64 || '';
    if (b64) return b64.startsWith('data:') ? b64 : 'data:image/png;base64,' + b64;
    if (frame.ImageFile) return frame.__base || frame.ImageFile; // dir-mode reference
    return '';
  }
  function pos(frame) {
    const p = frame.Position || frame.Cluster || null;
    if (!p) return { Left: 0, Top: 0, Width: 0, Height: 0 };
    return {
      Left: p.Left | 0, Top: p.Top | 0,
      Width: (p.Width != null ? p.Width : (p.Right - p.Left)) | 0,
      Height: (p.Height != null ? p.Height : (p.Bottom - p.Top)) | 0,
    };
  }
  function childIdx(frame) {
    const c = frame.Children || frame['Child Indices'] || frame.children || [];
    return Array.isArray(c) ? c.slice() : [];
  }

  // ── tree construction ───────────────────────────────────────────────────────
  // Find the root frame: the one no other frame lists as a child (fallback: 0).
  function findRoot(frames) {
    const referenced = new Set();
    frames.forEach(f => childIdx(f).forEach(i => referenced.add(i)));
    for (let i = 0; i < frames.length; i++) if (!referenced.has(i)) return i;
    return 0;
  }

  // Group a node's child frames into "structures": consecutive siblings sharing
  // the same Position rectangle are the cases of one multi-frame structure.
  function groupStructures(frames, indices) {
    const groups = [];
    const byKey = new Map();
    for (const i of indices) {
      const p = pos(frames[i]);
      const key = p.Left + ':' + p.Top + ':' + p.Width + ':' + p.Height;
      let g = byKey.get(key);
      if (!g) { g = { rect: p, cases: [] }; byKey.set(key, g); groups.push(g); }
      g.cases.push(i);
    }
    return groups;
  }

  // Decide where a child rectangle sits inside a parent's image. LabVIEW
  // GObject.Position is relative to the owning diagram, but exports vary between
  // absolute (top-level) and parent-relative coordinates, so detect per child.
  function placeWithin(parentRect, parentImgW, parentImgH, childRect) {
    const relL = childRect.Left - parentRect.Left;
    const relT = childRect.Top - parentRect.Top;
    const looksAbsolute =
      relL >= -2 && relT >= -2 &&
      relL <= parentImgW + 2 && relT <= parentImgH + 2;
    if (looksAbsolute) return { x: relL, y: relT };
    return { x: childRect.Left, y: childRect.Top };   // already parent-relative
  }

  // ── rendering ───────────────────────────────────────────────────────────────
  // Paint one frame's image into `layer`, then composite its child structures on
  // top. Recurses so nested structures live inside the case that owns them.
  function paintFrame(frames, frameIdx, layer, stageState) {
    const frame = frames[frameIdx];
    const myRect = pos(frame);

    const img = el('img', 'lvr-img', layer);
    img.src = imgSrc(frame);
    img.alt = frame.Label || 'diagram';
    img.draggable = false;

    const kids = childIdx(frame);
    if (!kids.length) return;

    // Wait for the parent image to measure, so children place against real pixels.
    const mountChildren = () => {
      const pw = img.naturalWidth || myRect.Width || layer.offsetWidth;
      const ph = img.naturalHeight || myRect.Height || layer.offsetHeight;
      for (const group of groupStructures(frames, kids)) {
        mountStructure(frames, group, layer, myRect, pw, ph, stageState);
      }
    };
    if (img.complete && img.naturalWidth) mountChildren();
    else img.addEventListener('load', mountChildren, { once: true });
  }

  // Build the in-place stepper for one structure (a group of same-rect cases).
  function mountStructure(frames, group, parentLayer, parentRect, pw, ph, stageState) {
    const place = placeWithin(parentRect, pw, ph, group.rect);
    const host = el('div', 'lvr-struct', parentLayer);
    host.style.left = place.x + 'px';
    host.style.top = place.y + 'px';
    host.style.width = (group.rect.Width || 0) + 'px';
    host.style.height = (group.rect.Height || 0) + 'px';

    // Each case gets its own absolutely-stacked layer; only one is shown. The
    // host is NOT clipped (overflow:visible): each case image is already sized to
    // the slot (100% x 100%), and leaving the host unclipped keeps every
    // structure's selector tab visible — it sits just ABOVE the frame, and nested
    // structures' tabs would otherwise be clipped by an ancestor structure.
    const caseLayers = group.cases.map((ci) => {
      const cl = el('div', 'lvr-case', host);
      paintFrame(frames, ci, cl, stageState);
      return cl;
    });

    const N = group.cases.length;
    let idx = 0;
    const single = N <= 1;

    // Selector chrome — anchored at the structure's top-left like the LabVIEW
    // subdiagram label / case selector. Hidden for single-frame "structures".
    const sel = el('div', 'lvr-sel' + (single ? ' lvr-sel--mono' : ''), host);
    const prev = el('button', 'lvr-sel__btn', sel); prev.type = 'button'; prev.textContent = '◀';
    prev.title = 'Previous case';
    const lbl = el('span', 'lvr-sel__lbl', sel);
    const next = el('button', 'lvr-sel__btn', sel); next.type = 'button'; next.textContent = '▶';
    next.title = 'Next case';

    function caseLabel(i) {
      const f = frames[group.cases[i]];
      const raw = (f && (f.Label || f.label || f.Name)) ? String(f.Label || f.label || f.Name) : '';
      const ord = N > 1 ? (i + 1) + '/' + N : '1';
      return raw ? `${ord}  ${raw}` : ord;
    }
    function show(i) {
      idx = (i + N) % N;
      caseLayers.forEach((cl, k) => { cl.style.display = k === idx ? 'block' : 'none'; });
      lbl.textContent = caseLabel(idx);
      prev.disabled = next.disabled = single;
      stageState.active = host;            // arrow keys drive the last-touched structure
    }
    prev.addEventListener('click', (e) => { e.stopPropagation(); show(idx - 1); });
    next.addEventListener('click', (e) => { e.stopPropagation(); show(idx + 1); });
    host.addEventListener('pointerdown', () => { stageState.active = host; host.__step = show.__self; });
    show.__self = (d) => show(idx + d);
    host.__step = show.__self;
    host.__sel = sel;
    show(0);
  }

  // ── pan / zoom stage ────────────────────────────────────────────────────────
  function wireStage(viewport, stage, stageState) {
    let zoom = 1, panX = 0, panY = 0, dragging = false, sx = 0, sy = 0, px = 0, py = 0;
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    function apply() {
      stage.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
      stageState.zoom = zoom;
    }
    function zoomAt(nz, ax, ay) {
      nz = clamp(nz, 0.04, 8);
      const r = viewport.getBoundingClientRect();
      const cx = (ax - r.left - panX) / zoom, cy = (ay - r.top - panY) / zoom;
      zoom = nz;
      panX = ax - r.left - cx * zoom;
      panY = ay - r.top - cy * zoom;
      apply();
    }
    viewport.addEventListener('wheel', (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      // Smooth, proportional zoom: gentle per tick and not runaway-fast on
      // trackpads (which emit many small wheel events).
      const d = Math.max(-50, Math.min(50, e.deltaY));
      zoomAt(zoom * Math.exp(-d * 0.002), e.clientX, e.clientY);
    }, { passive: false });
    viewport.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.lvr-sel') || e.target.closest('.lvr-reset')) return;  // let chrome buttons work
      dragging = true; sx = e.clientX; sy = e.clientY; px = panX; py = panY;
      viewport.classList.add('lvr-grabbing');
      try { viewport.setPointerCapture(e.pointerId); } catch (_) {}
    });
    viewport.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      panX = px + (e.clientX - sx); panY = py + (e.clientY - sy); apply();
    });
    const end = () => { dragging = false; viewport.classList.remove('lvr-grabbing'); };
    viewport.addEventListener('pointerup', end);
    viewport.addEventListener('pointercancel', end);
    viewport.addEventListener('dblclick', (e) => {
      // Double-clicking a case selector ('lvr-sel') or the Fit button ('lvr-reset')
      // must NOT zoom: paging quickly through a structure's cases lands two clicks
      // on the same arrow, which the browser reports as a dblclick that bubbles
      // here. The click handlers stopPropagation, but dblclick is a separate event,
      // so guard it the same way pointerdown does or the diagram randomly zooms in.
      if (e.target.closest('.lvr-sel') || e.target.closest('.lvr-reset')) return;
      zoomAt(zoom > 1 ? 1 : 2, e.clientX, e.clientY);
    });
    stageState.zoomAt = zoomAt;
    stageState.reset = () => { zoom = 1; panX = 0; panY = 0; apply(); };
    stageState.fit = (w, h) => {
      const r = viewport.getBoundingClientRect();
      if (!(r.width > 2 && r.height > 2 && w > 0 && h > 0)) return;   // viewport not laid out yet
      const pad = 24;
      // Fit the WHOLE diagram in view: never upscale past 1:1, but allow a tiny
      // zoom so even very large diagrams fit.
      zoom = clamp(Math.min((r.width - pad) / w, (r.height - pad) / h, 1), 0.04, 1);
      panX = Math.max(pad / 2, (r.width - w * zoom) / 2);
      panY = Math.max(pad / 2, (r.height - h * zoom) / 2);
      apply();
    };
    apply();
  }

  // ── public entry ────────────────────────────────────────────────────────────
  function render(container, frames, opts) {
    opts = opts || {};
    container.innerHTML = '';
    if (!Array.isArray(frames) || !frames.length) {
      const empty = el('div', 'lvr-empty', container);
      empty.textContent = 'No diagram frames to display.';
      return { destroy() { container.innerHTML = ''; } };
    }
    injectCss(container.ownerDocument);

    const viewport = el('div', 'lvr-viewport', container);
    const stage = el('div', 'lvr-stage', viewport);
    const root = el('div', 'lvr-layer lvr-root', stage);
    const stageState = { active: null, zoom: 1 };

    const rootIdx = (opts.rootIndex != null) ? opts.rootIndex : findRoot(frames);
    const rootRect = pos(frames[rootIdx]);
    // Size the stage to the root diagram so pan/zoom math has fixed bounds.
    const rootImg = new Image();
    rootImg.onload = () => {
      const w = rootImg.naturalWidth || rootRect.Width || 800;
      const h = rootImg.naturalHeight || rootRect.Height || 600;
      stage.style.width = w + 'px';
      stage.style.height = h + 'px';
      stageState.rootW = w; stageState.rootH = h;
      paintFrame(frames, rootIdx, root, stageState);
      // Fit now, and again next frame in case the viewport had not been laid out
      // when the image decoded, so the diagram ALWAYS opens fitted in view.
      const doFit = () => { if (stageState.fit) stageState.fit(w, h); };
      doFit();
      ((container.ownerDocument.defaultView) || window).requestAnimationFrame(doFit);
    };
    rootImg.src = imgSrc(frames[rootIdx]);

    wireStage(viewport, stage, stageState);

    // "Fit" button - returns the diagram to its default fitted size & position.
    const resetBtn = el('button', 'lvr-reset', viewport);
    resetBtn.type = 'button';
    resetBtn.title = 'Reset view \u2014 fit the whole diagram';
    resetBtn.textContent = 'Fit';
    resetBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (stageState.rootW && stageState.fit) stageState.fit(stageState.rootW, stageState.rootH);
    });

    function onKey(e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const h = stageState.active;
      if (!h || !h.__step) return;
      h.__step(e.key === 'ArrowRight' ? 1 : -1);
      e.preventDefault();
    }
    container.ownerDocument.addEventListener('keydown', onKey);

    return {
      destroy() {
        container.ownerDocument.removeEventListener('keydown', onKey);
        container.innerHTML = '';
      },
      reset() { if (stageState.rootW && stageState.fit) stageState.fit(stageState.rootW, stageState.rootH); },
    };
  }

  // ── styles (scoped under .lvr-viewport) ─────────────────────────────────────
  function injectCss(doc) {
    if (doc.getElementById('lvr-css')) return;
    const s = doc.createElement('style');
    s.id = 'lvr-css';
    s.textContent = `
.lvr-viewport{position:absolute;inset:0;overflow:hidden;background:#fff;
  background-image:radial-gradient(#e6e9ee 1px,transparent 1px);background-size:14px 14px;
  cursor:grab;touch-action:none}
.lvr-viewport.lvr-grabbing{cursor:grabbing}
.lvr-stage{position:absolute;top:0;left:0;transform-origin:0 0}
.lvr-layer{position:absolute;top:0;left:0}
.lvr-img{display:block;max-width:none;-webkit-user-drag:none;user-select:none}
.lvr-struct{position:absolute;outline:1px dashed rgba(31,111,235,.35);outline-offset:0}
.lvr-case{position:absolute;top:0;left:0;width:100%;height:100%}
.lvr-case .lvr-img{width:100%;height:100%}
.lvr-reset{position:absolute;top:10px;right:10px;z-index:6;border:1px solid #d0d7de;
  background:rgba(255,255,255,.92);color:#1f2328;cursor:pointer;
  font:600 12px/1 -apple-system,'Segoe UI',sans-serif;padding:6px 10px;border-radius:7px;
  box-shadow:0 1px 3px rgba(31,35,40,.18)}
.lvr-reset:hover{background:#eaeef2}
.lvr-sel{position:absolute;left:0;top:-22px;display:inline-flex;align-items:center;gap:2px;
  height:20px;padding:0 3px;background:#fffbe6;border:1px solid #d9c97a;border-bottom:none;
  border-radius:5px 5px 0 0;font:600 11px/1 -apple-system,'Segoe UI',sans-serif;color:#5a4b00;
  box-shadow:0 1px 2px rgba(0,0,0,.12);white-space:nowrap;z-index:5}
.lvr-sel--mono{background:#eef1f5;border-color:#cdd5df;color:#57606a}
.lvr-sel--mono .lvr-sel__btn{display:none}
.lvr-sel__btn{border:none;background:none;cursor:pointer;font-size:12px;line-height:1;
  padding:2px 4px;color:#5a4b00;border-radius:3px}
.lvr-sel__btn:hover:not(:disabled){background:#f1e6a8}
.lvr-sel__btn:disabled{opacity:.35;cursor:default}
.lvr-sel__lbl{padding:0 4px;min-width:30px;text-align:center;font-variant-numeric:tabular-nums}
.lvr-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:#8b949e;font:14px -apple-system,'Segoe UI',sans-serif}
/* The diagram viewing area stays WHITE even in dark mode: LabVIEW block diagrams
   are drawn on a white canvas, so a dark backdrop would clash with the PNGs and
   make wires/labels hard to read. Only the page chrome around it follows the theme. */`;
    (doc.head || doc.documentElement).appendChild(s);
  }

  global.LVRender = { render: render, findRoot: findRoot, groupStructures: groupStructures };
})(typeof window !== 'undefined' ? window : this);
