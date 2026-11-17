// The task map page: the app hands it a PR Lens graph document with show(), and it draws it with PrLens.render,
// narrowed to one repo when the repo chip picks one, with pan (drag or scroll) and zoom (pinch or ⌘-scroll).
// Messages to the app go through webkit.messageHandlers.wm: {type: "error", message} when the document can't be
// drawn, {type: "rendered"} when it was, and {type: "open", node, files: [{path, startLine?}], x, y, view} when a card
// with changed files is clicked (x, y: the click in the page's view; view: viewState(), to come back to).
(function () {
  "use strict";

  var LENSES = ["architecture", "data-flow", "data-model"];
  var CONTEXT_LANE = { id: "context", label: "Other repos" };

  /** The lenses the app can switch between: those the document declares, in PR Lens's order. */
  function lensesOf(doc) {
    return LENSES.filter(function (lens) { return (doc.lenses || []).indexOf(lens) >= 0; });
  }

  /** The part of `doc` in `repo` (a folder name; lane ids start with "<repo>/"): its lanes and their nodes, plus every
   * node elsewhere that an edge or a flow step links to one of them, in one extra "Other repos" lane, so a cross-repo
   * call stays visible. Edges and flow steps are kept when both ends are; a flow left with fewer than two
   * participants or no steps goes. Views, the walkthrough and layout hints name things that may be gone, so they go
   * too. Returns a new document; `doc` is left alone. */
  function filterToRepo(doc, repo) {
    var prefix = repo + "/";
    var lanes = doc.lanes.filter(function (lane) { return lane.id.indexOf(prefix) === 0; });
    var laneIds = new Set(lanes.map(function (lane) { return lane.id; }));
    var own = new Set();
    doc.nodes.forEach(function (node) { if (laneIds.has(node.lane)) own.add(node.id); });

    var context = new Set();
    function link(a, b) {
      if (own.has(a) && !own.has(b)) context.add(b);
      if (own.has(b) && !own.has(a)) context.add(a);
    }
    doc.edges.forEach(function (edge) { link(edge.from, edge.to); });
    (doc.flows || []).forEach(function (flow) {
      flow.messages.forEach(function (message) { link(message.from, message.to); });
    });

    function kept(id) { return own.has(id) || context.has(id); }
    var nodes = [];
    doc.nodes.forEach(function (node) {
      if (own.has(node.id)) nodes.push(node);
      else if (context.has(node.id)) nodes.push(Object.assign({}, node, { lane: CONTEXT_LANE.id }));
    });
    var flows = [];
    (doc.flows || []).forEach(function (flow) {
      var participants = flow.participants.filter(function (p) { return kept(p.node); });
      var messages = flow.messages.filter(function (m) { return kept(m.from) && kept(m.to); });
      if (participants.length >= 2 && messages.length > 0) {
        flows.push(Object.assign({}, flow, { participants: participants, messages: messages }));
      }
    });

    var out = Object.assign({}, doc, {
      lanes: context.size > 0 ? lanes.concat([Object.assign({}, CONTEXT_LANE)]) : lanes,
      nodes: nodes,
      edges: doc.edges.filter(function (edge) { return kept(edge.from) && kept(edge.to); }),
      flows: flows,
    });
    delete out.views;
    delete out.walkthrough;
    delete out.layout;
    return out;
  }

  /** The cards a click opens, as [{id, box, files}]: each node (and data-model table) the render's `atlas` boxes that
   * has at least one file in `changed` (workstream-relative paths), with only those files ({path, startLine?}). */
  function clickableCards(doc, atlas, changed) {
    var inDiff = new Set(changed);
    var cards = [];
    function add(items, boxes) {
      (items || []).forEach(function (item) {
        var box = boxes && boxes[item.id];
        if (!box) return;
        var files = [];
        (item.files || []).forEach(function (file) {
          if (!inDiff.has(file.path) || files.some(function (f) { return f.path === file.path; })) return;
          files.push(file.startLine ? { path: file.path, startLine: file.startLine } : { path: file.path });
        });
        if (files.length > 0) cards.push({ id: item.id, box: box, files: files });
      });
    }
    add(doc.nodes, atlas.nodes);
    add(doc.dataModel && doc.dataModel.tables, atlas.tables);
    return cards;
  }

  function inside(box, p) {
    return p.x >= box.x && p.x <= box.x + box.width && p.y >= box.y && p.y <= box.y + box.height;
  }

  /** The card at `p` (viewBox units), or null. A flow step is never a card, even inside the box of a node that heads
   * columns in two flows (the atlas boxes such a node with one rectangle covering both cards); where boxes overlap,
   * the smallest wins. */
  function cardAt(cards, atlas, p) {
    var steps = atlas.messages || {};
    for (var flow in steps) {
      for (var id in steps[flow]) { if (inside(steps[flow][id], p)) return null; }
    }
    var best = null;
    cards.forEach(function (card) {
      if (inside(card.box, p) && (!best || card.box.width * card.box.height < best.box.width * best.box.height)) {
        best = card;
      }
    });
    return best;
  }

  // ---------------------------------------------------------------------------------------------------------------
  // The page. Only set up in a browser: under node this file just exports the functions above.

  if (typeof document === "undefined") {
    if (typeof module !== "undefined") module.exports = { filterToRepo: filterToRepo, lensesOf: lensesOf, clickableCards: clickableCards, cardAt: cardAt };
    return;
  }

  var viewport = document.getElementById("viewport");
  var stage = document.getElementById("stage");
  var note = document.getElementById("note");
  var view = { x: 0, y: 0, scale: 1 };  // the stage's transform: pan in CSS pixels, then zoom
  var size = null;  // the drawing's {width, height}, viewBox units
  var shown = null;  // what the last show() drew: {lens, repo}
  var touched = false;  // panned or zoomed by hand since the last fit
  var state = { changed: [], atlas: null, cards: [] };  // for clicking a card through to its diff
  var hovered = null;  // the card under the pointer, outlined
  var MIN_SCALE = 0.05, MAX_SCALE = 4;

  function post(message) {
    var handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.wm;
    if (handler) handler.postMessage(message);
  }

  function fail(message) {
    post({ type: "error", message: String(message) });
  }

  function apply() {
    stage.style.transform = "translate(" + view.x + "px," + view.y + "px) scale(" + view.scale + ")";
  }

  /** The whole width in view, centred, from the top. */
  function fit() {
    if (!size) return;
    var pad = 12, width = Math.max(viewport.clientWidth - 2 * pad, 50);
    view.scale = clamp(width / size.width, MIN_SCALE, 1.25);  // a small map stays near its own size
    view.x = (viewport.clientWidth - size.width * view.scale) / 2;
    view.y = pad;
    touched = false;
    apply();
  }

  function clamp(n, lo, hi) { return Math.min(hi, Math.max(lo, n)); }

  /** Zoom by `factor`, keeping the point under (cx, cy) in place. */
  function zoomAt(cx, cy, factor) {
    var scale = clamp(view.scale * factor, MIN_SCALE, MAX_SCALE);
    var rect = viewport.getBoundingClientRect();
    var px = cx - rect.left, py = cy - rect.top;
    view.x = px - (px - view.x) * (scale / view.scale);
    view.y = py - (py - view.y) * (scale / view.scale);
    view.scale = scale;
    touched = true;
    apply();
  }

  function showNote(text) {
    note.textContent = text;
    document.body.classList.add("noting");
  }

  /** Draw `doc` (the JSON text wm sent, or the object) in `lens` and `theme` ("light" or "dark"), narrowed to
   * `repo` unless it is null. `changed`: the workstream-relative paths in the current diff. */
  function show(opts) {
    try {
      document.body.classList.toggle("dark", opts.theme === "dark");
      var parsed = PrLens.parse(typeof opts.doc === "string" ? opts.doc : JSON.stringify(opts.doc));
      if (!parsed.ok) {
        var issue = parsed.issues[0] || { path: "", message: "unknown problem" };
        fail((issue.path ? issue.path + ": " : "") + issue.message
             + (parsed.issues.length > 1 ? " (and " + (parsed.issues.length - 1) + " more)" : ""));
        return;
      }
      var doc = parsed.doc;
      var lenses = lensesOf(doc);
      if (lenses.length === 0) { fail("the map declares no lens this app can draw"); return; }
      var lens = lenses.indexOf(opts.lens) >= 0 ? opts.lens : lenses[0];
      var repo = opts.repo || null;
      state.changed = opts.changed || [];
      state.cards = [];
      hover(null);

      if (repo) {
        var narrowed = filterToRepo(doc, repo);
        if (narrowed.nodes.length === 0) {
          showNote("Nothing in this map is in " + repo + ".");
          post({ type: "rendered" });
          return;
        }
        if (lens === "data-flow" && narrowed.flows.length === 0) {
          showNote("No flow in this map involves " + repo + ".");
          post({ type: "rendered" });
          return;
        }
        var reparsed = PrLens.parse(JSON.stringify(narrowed));
        if (!reparsed.ok) {
          fail("narrowing the map to " + repo + " broke it: " + reparsed.issues[0].path + ": "
               + reparsed.issues[0].message);
          return;
        }
        doc = reparsed.doc;
      }

      var result = PrLens.render(doc, { lens: lens, theme: opts.theme === "dark" ? "dark" : "light" });
      stage.innerHTML = result.svg;
      var title = stage.querySelector("svg > title");  // otherwise the whole map shows it as a tooltip
      if (title) title.remove();
      state.atlas = result.atlas;
      state.cards = clickableCards(doc, result.atlas, state.changed);
      size = { width: result.width, height: result.height };
      document.body.classList.remove("noting");
      var refit = !shown || shown.lens !== lens || shown.repo !== repo;
      shown = { lens: lens, repo: repo };
      if (refit) fit(); else apply();
      post({ type: "rendered" });
    } catch (error) {
      fail(error && error.message ? error.message : error);
    }
  }

  /** The point under (clientX, clientY) in the drawing's viewBox units. */
  function toViewBox(clientX, clientY) {
    var svg = stage.querySelector("svg");
    var ctm = svg && svg.getScreenCTM();
    if (!ctm) return null;
    var point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    return point.matrixTransform(ctm.inverse());
  }

  function cardUnder(event) {
    if (!state.atlas || state.cards.length === 0) return null;
    var p = toViewBox(event.clientX, event.clientY);
    return p ? cardAt(state.cards, state.atlas, p) : null;
  }

  /** Outline `card` (null: none) and give it the pointer. */
  function hover(card) {
    if (card === hovered) return;
    hovered = card;
    var old = stage.querySelector("rect.wm-hover");
    if (old) old.remove();
    viewport.classList.toggle("over-card", !!card);
    var svg = stage.querySelector("svg");
    if (!card || !svg) return;
    var pad = 3;
    var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("class", "wm-hover");
    rect.setAttribute("x", card.box.x - pad);
    rect.setAttribute("y", card.box.y - pad);
    rect.setAttribute("width", card.box.width + 2 * pad);
    rect.setAttribute("height", card.box.height + 2 * pad);
    rect.setAttribute("rx", 10);
    svg.appendChild(rect);
  }

  // Pan by dragging; a click that hardly moved opens the card under it.
  var drag = null;
  viewport.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    drag = { id: event.pointerId, x: event.clientX - view.x, y: event.clientY - view.y,
             startX: event.clientX, startY: event.clientY, moved: false };
    try { viewport.setPointerCapture(event.pointerId); } catch (e) { /* a pointer the page can't capture */ }
    viewport.classList.add("dragging");
  });
  viewport.addEventListener("pointermove", function (event) {
    if (!drag) { hover(cardUnder(event)); return; }
    if (event.pointerId !== drag.id) return;
    if (Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 4) drag.moved = true;
    if (!drag.moved) return;
    view.x = event.clientX - drag.x;
    view.y = event.clientY - drag.y;
    touched = true;
    apply();
  });
  viewport.addEventListener("pointerleave", function () { if (!drag) hover(null); });
  function endDrag(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var clicked = !drag.moved && event.type === "pointerup";
    drag = null;
    viewport.classList.remove("dragging");
    if (!clicked) return;
    var card = cardUnder(event);
    if (card) {
      var rect = viewport.getBoundingClientRect();
      post({ type: "open", node: card.id, files: card.files, x: event.clientX - rect.left,
             y: event.clientY - rect.top, view: window.wmMap.viewState() });
    }
  }
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);
  viewport.addEventListener("dblclick", fit);

  // Scroll pans; ⌘-scroll zooms, and so does a pinch, which WebKit sends as gesture events and/or ctrl-wheel.
  var pinching = null;
  viewport.addEventListener("wheel", function (event) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) {
      if (pinching !== null) return;  // the gesture events have it
      zoomAt(event.clientX, event.clientY, Math.exp(-clamp(event.deltaY, -50, 50) * 0.01));
    } else {
      view.x -= event.deltaX;
      view.y -= event.deltaY;
      touched = true;
      apply();
    }
    hover(cardUnder(event));
  }, { passive: false });
  viewport.addEventListener("gesturestart", function (event) {
    event.preventDefault();
    pinching = 1;
  });
  viewport.addEventListener("gesturechange", function (event) {
    event.preventDefault();
    if (pinching === null) return;
    zoomAt(event.clientX, event.clientY, event.scale / pinching);
    pinching = event.scale;
  });
  viewport.addEventListener("gestureend", function (event) {
    event.preventDefault();
    pinching = null;
  });

  window.addEventListener("resize", function () { if (!touched) fit(); });

  window.wmMap = {
    show: show,
    /** Pan and zoom, to put back with restore(). */
    viewState: function () { return { x: view.x, y: view.y, scale: view.scale }; },
    restore: function (saved) {
      if (!saved) return;
      view.x = saved.x;
      view.y = saved.y;
      view.scale = clamp(saved.scale, MIN_SCALE, MAX_SCALE);
      touched = true;
      apply();
    },
    filterToRepo: filterToRepo,
    state: state,
  };
})();
