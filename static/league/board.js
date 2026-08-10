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
  var mockBtn = document.getElementById('mock-btn');       // absent with no team
  var undoBtn = document.getElementById('undo-pick-btn');
  var modal = document.getElementById('pick-modal');
  var list = document.getElementById('pick-list');
  var search = document.getElementById('pick-search');
  var hint = document.getElementById('pick-hint');
  var countLabel = document.getElementById('pick-count');
  var title = document.getElementById('pick-modal-title');
  var filters = document.getElementById('pick-filters');

  /* Mock-draft state, declared here for the same reason: clearing the
   * simulation reads it, and that can happen during start-up, before the
   * mock-draft section further down would have assigned anything. */
  var chosen = [];            // [[pick_id, player_id], ...] in the order I made them
  var mockActive = false;
  var pending = null;         // the pick the server is waiting on
  var poolAvailable = [];     // everything still undrafted, ADP order, for search
  var filterPos = '';         // '' = the server's suggestions, 'ALL' or a position

  /* Same lifetime and the same reasoning as the sandbox stash: sessionStorage,
   * keyed by season, dead when the tab closes. A mock draft is a scratchpad
   * built on a keeper plan we deliberately never write down. */
  var mockKey = 'keeper-mock-' + (sandbox ? sandbox.dataset.season || '' : '');

  var MAX_KEEPERS = 3;
  var SEARCH_RESULT_LIMIT = 60;
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
    // it. A mock draft is worse than stale: a newly burned cell may be one I
    // had already drafted at, which the server would rightly refuse. So this
    // one clears without asking, and says so rather than vanishing quietly.
    var hadPicks = mockActive && chosen.length;
    clearSimulation(false);
    if (hadPicks) {
      showProblem('Keeper selection changed, so the mock draft was reset — ' +
                  'your picks depend on which cells are burned.');
    }

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

  function wipeFills() {
    // Only the projection comes off. Locked predictions are server-rendered
    // and untouched; the sandbox ticks and their burned cells stay exactly as
    // they were.
    document.querySelectorAll('.cell.projected, .cell.mine-pick').forEach(function (cell) {
      cell.classList.remove('filled', 'projected', 'mine-pick');
      stripPositions(cell);
      var fill = cell.querySelector('.sim-fill');
      if (fill) fill.remove();
    });
    clearBtn.hidden = true;
  }

  function resetMock() {
    mockActive = false;
    chosen = [];
    pending = null;
    poolAvailable = [];
    saveMock();
    closeModal();
    updateMockButtons();
  }

  /* Clear the projection. Returns false if the user backed out.
   *
   * Mock picks are the one thing here that took real effort to produce, so
   * throwing them away asks first -- unlike the auto projection, which is one
   * button press to rebuild. */
  function clearSimulation(ask) {
    if (ask && mockActive && chosen.length) {
      var message = 'Clear the mock draft too? Your ' + chosen.length +
        ' pick(s) would have to be made again.';
      if (!window.confirm(message)) return false;
    }

    wipeFills();
    if (mockActive) resetMock();
    return true;
  }

  function paintFill(fill) {
    var cell = cellFor(fill.pick_id);
    // A cell already holding a keeper, a locked call or a sandbox burn keeps
    // it. The server does not project into those, but painting over one would
    // be the worst kind of bug -- a fact quietly replaced by a guess.
    if (!cell || cell.classList.contains('locked') || cell.classList.contains('burned')) {
      return;
    }

    // Hue is position; the ring is certainty. A pick I made myself is a
    // decision, not a projection, so it gets the full colour and the same solid
    // ring a lock wears -- while auto-simmed cells stay desaturated and ringless.
    cell.classList.add('filled', fill.source === 'manual' ? 'mine-pick' : 'projected');
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

  function showProblem(message) {
    // Say so. A board that simply stayed empty would read as "the simulation
    // found nothing", which is a different and wrong story.
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function simulate() {
    // Full auto is a different mode, so it takes the mock draft with it -- but
    // only with permission.
    if (!clearSimulation(true)) return;

    errorBox.hidden = true;
    errorBox.className = 'sim-error';
    simulateBtn.disabled = true;
    simulateBtn.textContent = 'Simulating…';

    // The sandbox selection goes in the POST body and nowhere else -- see
    // views.simulate for why it must never reach a URL.
    post(controls.dataset.simulateUrl, { entry_ids: selectedIds() })
      .then(function (data) {
        data.fills.forEach(paintFill);
        clearBtn.hidden = data.fills.length === 0;
      })
      .catch(function (err) {
        showProblem('Could not simulate the draft: ' + err.message);
      })
      .then(function () {
        simulateBtn.disabled = false;
        simulateBtn.textContent = 'Simulate draft';
      });
  }

  simulateBtn.addEventListener('click', simulate);
  clearBtn.addEventListener('click', function () { clearSimulation(true); });

  /* --- Mock draft --------------------------------------------------------- */

  /* "I make my picks": the same projection, paused at each of my cells.
   *
   * The server holds no cursor and saves nothing, so `chosen` below IS the
   * draft. Every step posts the whole list and the server replays from scratch;
   * that is why undo is `chosen.pop()` and nothing else. See views.simulate.
   *
   * Everything in here is presentation and bookkeeping. No pick logic: which
   * players are legal, who the other teams take and where the draft pauses are
   * all draft_sim's answers, arriving in the step response. */
  function saveMock() {
    try {
      sessionStorage.setItem(mockKey, JSON.stringify({ active: mockActive, chosen: chosen }));
    } catch (e) { /* private mode: the draft just does not survive a reload */ }
  }

  function restoreMock() {
    try {
      var saved = JSON.parse(sessionStorage.getItem(mockKey) || 'null');
      if (saved && saved.active && Array.isArray(saved.chosen)) {
        chosen = saved.chosen;
        mockActive = true;
      }
    } catch (e) { /* ignore a corrupt stash rather than break the board */ }
  }

  function manualMap() {
    var map = {};
    chosen.forEach(function (pair) { map[pair[0]] = pair[1]; });
    return map;
  }

  function updateMockButtons() {
    if (!mockBtn) return;
    mockBtn.textContent = mockActive ? 'Continue draft' : 'Mock draft — I pick';
    undoBtn.hidden = !(mockActive && chosen.length);
    undoBtn.textContent = 'Undo my last pick';
  }

  /* One step: post what I have chosen, paint what comes back, and either open
   * the modal on my next pick or report the draft finished. */
  function step() {
    errorBox.hidden = true;
    errorBox.className = 'sim-error';
    mockBtn.disabled = true;
    undoBtn.disabled = true;

    return post(controls.dataset.simulateUrl, {
      mock: true,
      manual_picks: manualMap(),
      entry_ids: selectedIds()
    })
      .then(function (data) {
        wipeFills();
        data.fills.forEach(paintFill);
        clearBtn.hidden = data.fills.length === 0;

        pending = data.your_pick;
        poolAvailable = data.available || [];

        if (pending) {
          openModal();
        } else {
          closeModal();
          showProblem('Mock draft complete — every one of your picks is made.');
          errorBox.className = 'sim-done';
        }
        updateMockButtons();
      })
      .catch(function (err) {
        showProblem('Could not run the mock draft: ' + err.message);
      })
      .then(function () {
        mockBtn.disabled = false;
        undoBtn.disabled = false;
      });
  }

  function startMock() {
    mockActive = true;
    errorBox.className = 'sim-error';
    saveMock();
    updateMockButtons();
    step();
  }

  function undoLastPick() {
    if (!chosen.length) return;
    chosen.pop();               // the entire undo: replay does the rest
    saveMock();
    step();
  }

  function choose(playerId) {
    if (!pending) return;
    chosen.push([pending.pick_id, playerId]);
    saveMock();
    step();
  }

  /* --- the pick modal --- */

  /* --- where the panel sits --- */

  /* Auto-anchored under the cell being drafted, so the rounds above it -- the
   * ones that just happened, and the ones you are reacting to -- stay readable.
   * Dragging is remembered as an OFFSET from that anchor rather than as an
   * absolute position: the next pick is a different cell, often elsewhere on
   * the page, and a remembered absolute position would strand the panel
   * off-screen. Keeping the offset preserves "a bit to the right and lower",
   * which is what someone who moved it actually meant. */
  var dragOffset = { x: 0, y: 0 };
  var ANCHOR_GAP = 10;          // px between the cell and the panel
  var EDGE_MARGIN = 8;          // keep this clear of the viewport edges

  function placePanel() {
    var cell = cellFor(pending.pick_id);
    var moved = dragOffset.x || dragOffset.y;

    if (!cell) {
      // The pick is in a round the board is not currently showing. Park the
      // panel in view rather than at coordinates for a cell that is not there.
      modal.classList.add('detached');
      modal.style.left = (window.scrollX + EDGE_MARGIN) + 'px';
      modal.style.top = (window.scrollY + 80) + 'px';
      return;
    }

    cell.classList.add('on-the-clock');

    // Page coordinates, so the panel scrolls with the board it is pinned to.
    var box = cell.getBoundingClientRect();
    var left = box.left + window.scrollX + dragOffset.x;
    var top = box.bottom + window.scrollY + ANCHOR_GAP + dragOffset.y;

    // Clamp horizontally: the rightmost columns would otherwise put half the
    // panel past the edge of the window.
    var maxLeft = window.scrollX + document.documentElement.clientWidth
      - modal.offsetWidth - EDGE_MARGIN;
    left = Math.max(window.scrollX + EDGE_MARGIN, Math.min(left, maxLeft));

    modal.style.left = left + 'px';
    modal.style.top = top + 'px';

    // The little tail points at the cell, so keep it over the cell even after
    // the clamp has slid the panel sideways.
    var tail = box.left + window.scrollX - left + (box.width / 2);
    modal.style.setProperty('--tail-x', Math.max(12, Math.min(tail, modal.offsetWidth - 20)) + 'px');
    modal.classList.toggle('detached', Boolean(moved));

    // Put the cell in the middle of the screen, which leaves the rounds above
    // it visible and room below for the panel.
    cell.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  function clearOnTheClock() {
    document.querySelectorAll('.cell.on-the-clock').forEach(function (cell) {
      cell.classList.remove('on-the-clock');
    });
  }

  function startDrag(event) {
    if (event.target.closest('.modal-close')) return;

    var box = modal.getBoundingClientRect();
    var grabX = event.clientX - box.left;
    var grabY = event.clientY - box.top;
    var anchorLeft = parseFloat(modal.style.left) - dragOffset.x;
    var anchorTop = parseFloat(modal.style.top) - dragOffset.y;

    modal.classList.add('dragging');
    document.body.classList.add('dragging-panel');

    function move(moveEvent) {
      var left = moveEvent.clientX - grabX + window.scrollX;
      var top = moveEvent.clientY - grabY + window.scrollY;
      modal.style.left = left + 'px';
      modal.style.top = top + 'px';
      // Store where this ended up RELATIVE to the auto-anchor, so the next
      // pick opens in the same relative spot.
      dragOffset = { x: left - anchorLeft, y: top - anchorTop };
    }

    function stop() {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', stop);
      modal.classList.remove('dragging');
      document.body.classList.remove('dragging-panel');
      modal.classList.add('detached');
    }

    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', stop);
    event.preventDefault();
  }

  function openModal() {
    // The label is "3.4" -- round, then position within the round. The header
    // reads better spelled out than as a dotted pick number.
    var inRound = String(pending.label).split('.')[1] || '';
    title.textContent = 'Round ' + pending.round + ', pick ' + inRound + ' — your pick';
    filterPos = '';
    search.value = '';
    setActiveChip('');
    renderList();

    clearOnTheClock();
    modal.hidden = false;
    placePanel();               // needs to be visible first, to have a width
    search.focus({ preventScroll: true });
  }

  function closeModal() {
    // Deliberately does NOT abandon the draft: the sim stays paused here and
    // "Continue draft" reopens exactly this pick.
    if (modal) modal.hidden = true;
    clearOnTheClock();
  }

  function setActiveChip(pos) {
    Array.prototype.forEach.call(filters.querySelectorAll('.chip'), function (chip) {
      chip.classList.toggle('active', chip.dataset.pos === pos);
    });
  }

  function visiblePlayers() {
    var term = search.value.trim().toLowerCase();

    // No search and no filter: show exactly what the server suggested, caps and
    // all. Touching either widens the list to the whole available pool, on
    // purpose -- the caps are advice about my own team, not a rule.
    if (!term && filterPos === '') return pending.suggestions;

    return poolAvailable.filter(function (player) {
      if (filterPos && filterPos !== 'ALL' && player.position !== filterPos) return false;
      return !term || player.name.toLowerCase().indexOf(term) !== -1;
    }).slice(0, SEARCH_RESULT_LIMIT);
  }

  function renderList() {
    var players = visiblePlayers();
    var suggesting = !search.value.trim() && filterPos === '';

    hint.textContent = suggesting
      ? 'Best available that fits your roster. Search or filter to see everyone.'
      : 'Everything still available — the positional caps do not apply to your own picks.';

    // The board shows rounds 1-8 by default, so a later pick has no cell to
    // sit under. Say so, rather than leaving the panel floating unexplained.
    if (!cellFor(pending.pick_id)) {
      hint.textContent += ' This pick is in a hidden round — "Show all rounds" puts it on the board.';
    }

    countLabel.textContent = players.length + ' shown of ' + poolAvailable.length + ' available';

    list.textContent = '';
    if (!players.length) {
      var none = document.createElement('li');
      none.className = 'pick-none';
      none.textContent = 'Nobody matches that.';
      list.appendChild(none);
      return;
    }

    players.forEach(function (player) {
      var row = document.createElement('li');

      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'pick-row';
      button.addEventListener('click', function () { choose(player.id); });

      var pos = document.createElement('span');
      pos.className = 'pos pos-' + player.position;
      pos.textContent = player.position;

      var name = document.createElement('span');
      name.className = 'pick-name';
      name.textContent = player.name;

      var team = document.createElement('span');
      team.className = 'pick-team';
      team.textContent = player.nfl_team || '';

      var adp = document.createElement('span');
      adp.className = 'pick-adp';
      adp.textContent = player.adp === null ? 'no ADP' : 'ADP ' + player.adp;

      button.appendChild(pos);
      button.appendChild(name);
      button.appendChild(team);
      button.appendChild(adp);
      row.appendChild(button);
      list.appendChild(row);
    });
  }

  if (mockBtn) {
    mockBtn.addEventListener('click', function () {
      if (mockActive) { step(); } else { startMock(); }
    });
    undoBtn.addEventListener('click', undoLastPick);

    document.getElementById('pick-modal-close').addEventListener('click', closeModal);
    document.getElementById('auto-pick-btn').addEventListener('click', function () {
      // Best available under the caps -- the server already worked that out, so
      // this is the first suggestion and no second opinion in the browser.
      if (pending && pending.suggestions.length) choose(pending.suggestions[0].id);
    });

    search.addEventListener('input', renderList);
    filters.addEventListener('click', function (event) {
      var chip = event.target.closest('.chip');
      if (!chip) return;
      filterPos = chip.dataset.pos;
      setActiveChip(filterPos);
      renderList();
    });

    document.getElementById('pick-modal-drag').addEventListener('mousedown', startDrag);

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !modal.hidden) closeModal();
    });

    // Reflow if the window changes size while a pick is open -- the clamp
    // against the right edge depends on the viewport width.
    window.addEventListener('resize', function () {
      if (!modal.hidden && pending) placePanel();
    });

    /* Out of the page flow entirely. The panel is positioned in page
     * coordinates, and living inside <main> would make it a hostage to any
     * ancestor that grows a transform or its own positioning context -- and
     * .board-scroll, right next to it, already clips its overflow. */
    document.body.appendChild(modal);

    restoreMock();
    updateMockButtons();
  }

  /* --- Reset ------------------------------------------------------------- */

  /* The locks are rows in the database and the server deletes them; the sandbox
   * ticks only ever existed in this tab, so nothing on the server can clear
   * them. Both halves have to happen for "reset" to mean what it says. */
  var resetForm = document.getElementById('reset-board-form');

  if (resetForm) {
    resetForm.addEventListener('submit', function (event) {
      var lockCount = document.querySelectorAll('.cell.predicted').length;
      var message = lockCount
        ? 'Clear all ' + lockCount + ' locked prediction(s) and your keeper ticks?'
        : 'Clear your keeper ticks?';

      if (!window.confirm(message)) {
        event.preventDefault();
        return;
      }

      // Wipe the stash BEFORE the POST: the response is a redirect back here,
      // and the restore on the next load would otherwise tick everything
      // straight back on.
      boxes.forEach(function (box) { box.checked = false; });
      saveSelection();
      resetMock();
    });
  }
})();
