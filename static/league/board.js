/* Draft board: the keeper sandbox and the ADP simulation.
 *
 * Two features, one file, no framework and no build step.
 *
 *   Sandbox     -- ticking players POSTs the selection to /api/keeper-preview/,
 *                  which runs the same keeper_engine code the commissioner's
 *                  admin uses, and paints the picks that set would burn.
 *   Simulation  -- "Simulate draft" POSTs to /board/simulate/ and paints a
 *                  projected player into every cell nobody has spoken for.
 *
 * Nothing either one does is ever saved. Declarations go to the commissioner
 * by text; the simulation is recomputed on demand.
 */
(function () {
  'use strict';

  var controls = document.getElementById('board-controls');
  if (!controls) return;                    // not the board page

  var sandbox = document.getElementById('sandbox');   // absent post-reveal

  /* Every element this file touches is looked up HERE, before any of the
   * behaviour below runs. The two halves call into each other -- restoring the
   * sandbox on load clears a stale projection -- and a lookup left further down
   * the file is only hoisted as `undefined`, so that call would throw and take
   * the rest of the script down with it. */
  var resultBox = document.getElementById('sandbox-result');
  var simulateBtn = document.getElementById('simulate-btn');
  var clearBtn = document.getElementById('clear-sim-btn');
  var errorBox = document.getElementById('sim-error');

  var MAX_KEEPERS = 3;
  var POSITIONS = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF'];

  /* Django's CSRF check is a double-submit: the server compares this token
   * against the csrftoken cookie. fetch() sends the cookie automatically but
   * knows nothing about the token, so we read the one the {% csrf_token %} tag
   * rendered and pass it as a header ourselves. A cross-site page can trigger
   * the cookie but cannot read this value, which is what makes the pair safe. */
  var tokenField = controls.querySelector('[name=csrfmiddlewaretoken]');
  var csrfToken = tokenField ? tokenField.value : '';

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken
      },
      credentials: 'same-origin',           // send the session + csrf cookies
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || 'Request failed.');
        return data;
      });
    });
  }

  function stripPositions(node) {
    if (!node) return;
    POSITIONS.forEach(function (pos) { node.classList.remove('pos-bg-' + pos); });
  }

  function cellFor(pickId) {
    return document.querySelector('.cell[data-pick-id="' + pickId + '"]');
  }

  /* --- Sandbox ----------------------------------------------------------- */

  var boxes = sandbox
    ? Array.prototype.slice.call(sandbox.querySelectorAll('.keeper-choice'))
    : [];

  function selectedIds() {
    return boxes.filter(function (b) { return b.checked; })
                .map(function (b) { return parseInt(b.value, 10); });
  }

  /* Locking a prediction is a form POST and a redirect, which reloads the page
   * and empties these checkboxes. So the ticks are stashed in the browser.
   *
   * sessionStorage, NOT localStorage: it dies with the tab. This is the
   * manager's real keeper plan, which the whole design keeps out of the
   * database (rules section 1) -- the shortest lifetime that still survives a
   * redirect is the right one, and a shared computer forgets it on close.
   * Keyed by season so next year starts clean. */
  var storageKey = sandbox ? 'keeper-sandbox-' + (sandbox.dataset.season || '') : null;

  function saveSelection() {
    if (!storageKey) return;
    // Private-browsing modes can throw on write. Losing the ticks is a small
    // annoyance; a thrown exception would take the whole sandbox down with it.
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(selectedIds()));
    } catch (e) { /* no storage available -- carry on without it */ }
  }

  function restoreSelection() {
    if (!storageKey) return false;

    var saved;
    try {
      saved = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    } catch (e) {
      return false;
    }
    if (!Array.isArray(saved) || saved.length === 0) return false;

    // Match on the ids still rendered: a player dropped from the roster since
    // the tick simply does not come back. Capped, so a hand-edited store can
    // never push an over-limit set at the server.
    var wanted = saved.slice(0, MAX_KEEPERS).map(String);
    boxes.forEach(function (box) {
      box.checked = wanted.indexOf(box.value) !== -1;
    });

    return selectedIds().length > 0;
  }

  function enforceMax() {
    var atLimit = selectedIds().length >= MAX_KEEPERS;
    boxes.forEach(function (b) {
      // Server-side validation is the real limit; this just stops the UI
      // offering a fourth pick that would only come back as an error.
      b.disabled = atLimit && !b.checked;
      b.closest('li').classList.toggle('disabled', b.disabled);
    });
  }

  /* Colour on this board means ONE thing: position. A burned cell wears the
   * same .pos-bg-* class a filled cell would, so a kept RB is RB-teal here and
   * everywhere else. What separates a sandbox burn from a locked call is the
   * ring (dashed vs solid), not the hue.
   *
   * The position comes off the checkbox's data-position attribute rather than
   * from the server's response, so /api/keeper-preview/ stays a pure keeper-math
   * endpoint with no presentation concerns in it. */
  var positionByEntry = {};
  boxes.forEach(function (box) {
    positionByEntry[box.value] = box.dataset.position || '';
  });

  function posClass(entryId) {
    var pos = positionByEntry[String(entryId)];
    return POSITIONS.indexOf(pos) === -1 ? '' : 'pos-bg-' + pos;
  }

  function clearBoard() {
    document.querySelectorAll('.cell.burned').forEach(function (cell) {
      cell.classList.remove('burned');
      stripPositions(cell);
      cell.removeAttribute('title');
      var tag = cell.querySelector('.burn-tag');
      if (tag) tag.remove();
    });
  }

  function paintSwatches() {
    boxes.forEach(function (box) {
      var item = box.closest('li');
      var swatch = item.querySelector('.swatch');
      item.classList.remove('picked');
      stripPositions(swatch);
      if (box.checked) {
        item.classList.add('picked');
        var cls = posClass(box.value);
        if (swatch && cls) swatch.classList.add(cls);
      }
    });
  }

  function paintBurn(burn) {
    var cell = cellFor(burn.pick_id);
    if (!cell) return;

    cell.classList.add('burned');
    var cls = posClass(burn.entry_id);
    if (cls) cell.classList.add(cls);

    cell.title = burn.via === 'base cost'
      ? burn.player + ' costs Round ' + burn.cost_round
      : burn.player + ' costs Round ' + burn.cost_round +
        ', moved here (' + burn.via + ')';

    var tag = document.createElement('span');
    tag.className = 'burn-tag';
    tag.textContent = burn.player;
    if (burn.via !== 'base cost') {
      var via = document.createElement('em');
      via.textContent = burn.via;
      tag.appendChild(via);
    }
    cell.appendChild(tag);
  }

  function render(data) {
    clearBoard();
    paintSwatches();
    data.burned.forEach(paintBurn);

    var parts = [];
    if (data.errors.length) {
      parts.push('<p class="sandbox-bad">That set is not legal:</p><ul>' +
        data.errors.map(li).join('') + '</ul>');
    } else if (data.burned.length) {
      parts.push('<p class="sandbox-good">Legal set. Burns ' +
        data.burned.map(function (b) { return 'Round ' + b.round; }).join(', ') +
        '.</p>');
    }
    if (data.warnings.length) {
      parts.push('<ul class="sandbox-warn">' + data.warnings.map(li).join('') + '</ul>');
    }

    resultBox.innerHTML = parts.join('');
    resultBox.hidden = parts.length === 0;
  }

  function li(text) {
    // Build via textContent so engine strings are never treated as markup.
    var node = document.createElement('li');
    node.textContent = text;
    return node.outerHTML;
  }

  function showError(message) {
    clearBoard();
    resultBox.innerHTML = '';
    var p = document.createElement('p');
    p.className = 'sandbox-bad';
    p.textContent = message;
    resultBox.appendChild(p);
    resultBox.hidden = false;
  }

  function preview() {
    enforceMax();
    var ids = selectedIds();

    saveSelection();
    paintSwatches();
    // Any projection on screen was computed from the previous selection, so it
    // is now wrong -- a keeper burns a pick, which moves everyone drafted after
    // it. Drop it rather than leave a stale board up; the button re-runs it.
    clearSimulation();

    if (ids.length === 0) {
      clearBoard();
      resultBox.hidden = true;
      return;
    }

    post(sandbox.dataset.previewUrl, { entry_ids: ids })
      .then(render)
      .catch(function (err) { showError(err.message); });
  }

  boxes.forEach(function (box) { box.addEventListener('change', preview); });

  // Re-run the preview after restoring, so the sidebar verdict and the burned
  // cells match the ticks rather than lagging a page behind them.
  if (restoreSelection()) {
    preview();
  } else {
    enforceMax();
  }

  /* --- Simulation -------------------------------------------------------- */

  function clearSimulation() {
    // Only the projection comes off. Locked predictions are server-rendered
    // and untouched; the sandbox ticks and their burned cells stay exactly as
    // they were.
    document.querySelectorAll('.cell.projected').forEach(function (cell) {
      cell.classList.remove('filled', 'projected');
      stripPositions(cell);
      var fill = cell.querySelector('.sim-fill');
      if (fill) fill.remove();
    });
    clearBtn.hidden = true;
  }

  function paintFill(fill) {
    var cell = cellFor(fill.pick_id);
    // A cell already holding a keeper, a locked call or a sandbox burn keeps
    // it. The server does not project into those, but painting over one would
    // be the worst kind of bug -- a fact quietly replaced by a guess.
    if (!cell || cell.classList.contains('locked') || cell.classList.contains('burned')) {
      return;
    }

    cell.classList.add('filled', 'projected');
    if (POSITIONS.indexOf(fill.position) !== -1) {
      cell.classList.add('pos-bg-' + fill.position);
    }

    var wrap = document.createElement('span');
    wrap.className = 'sim-fill';

    var name = document.createElement('span');
    name.className = 'filled-name';
    name.textContent = fill.player;

    var sub = document.createElement('span');
    sub.className = 'filled-sub';
    sub.textContent = fill.nfl_team ? fill.position + ' – ' + fill.nfl_team : fill.position;

    wrap.appendChild(name);
    wrap.appendChild(sub);
    cell.appendChild(wrap);
  }

  function simulate() {
    errorBox.hidden = true;
    simulateBtn.disabled = true;
    simulateBtn.textContent = 'Simulating…';

    clearSimulation();

    // The sandbox selection goes in the POST body and nowhere else -- see
    // views.simulate for why it must never reach a URL.
    post(controls.dataset.simulateUrl, { entry_ids: selectedIds() })
      .then(function (data) {
        data.fills.forEach(paintFill);
        clearBtn.hidden = data.fills.length === 0;
      })
      .catch(function (err) {
        // Say so. A board that simply stayed empty would read as "the
        // simulation found nothing", which is a different and wrong story.
        errorBox.textContent = 'Could not simulate the draft: ' + err.message;
        errorBox.hidden = false;
      })
      .then(function () {
        simulateBtn.disabled = false;
        simulateBtn.textContent = 'Simulate draft';
      });
  }

  simulateBtn.addEventListener('click', simulate);
  clearBtn.addEventListener('click', clearSimulation);
})();
