/* Draft board keeper sandbox.
 *
 * Ticking players POSTs the selection to /api/keeper-preview/, which runs the
 * same keeper_engine code the commissioner's admin uses, and paints the picks
 * that set would burn. Nothing is ever saved -- declarations go to the
 * commissioner by text.
 *
 * No framework, no build step: one file, plain DOM APIs.
 */
(function () {
  'use strict';

  var sandbox = document.getElementById('sandbox');
  if (!sandbox) return;                     // no team linked, or keepers revealed

  var MAX_KEEPERS = 3;
  var url = sandbox.dataset.previewUrl;
  var resultBox = document.getElementById('sandbox-result');
  var boxes = Array.prototype.slice.call(sandbox.querySelectorAll('.keeper-choice'));

  /* Django's CSRF check is a double-submit: the server compares this token
   * against the csrftoken cookie. fetch() sends the cookie automatically but
   * knows nothing about the token, so we read the one the {% csrf_token %} tag
   * rendered and pass it as a header ourselves. A cross-site page can trigger
   * the cookie but cannot read this value, which is what makes the pair safe. */
  var tokenField = sandbox.querySelector('[name=csrfmiddlewaretoken]');
  var csrfToken = tokenField ? tokenField.value : '';

  function selectedIds() {
    return boxes.filter(function (b) { return b.checked; })
                .map(function (b) { return parseInt(b.value, 10); });
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

  function clearBoard() {
    document.querySelectorAll('.cell.burned').forEach(function (cell) {
      cell.classList.remove('burned');
      cell.removeAttribute('title');
      var tag = cell.querySelector('.burn-tag');
      if (tag) tag.remove();
    });
  }

  function paintBurn(burn) {
    var cell = document.querySelector('.cell[data-pick-id="' + burn.pick_id + '"]');
    if (!cell) return;

    cell.classList.add('burned');

    var why = burn.via === 'base cost'
      ? burn.player + ' costs Round ' + burn.cost_round
      : burn.player + ' costs Round ' + burn.cost_round +
        ', moved here (' + burn.via + ')';
    cell.title = why;

    var tag = document.createElement('span');
    tag.className = 'burn-tag';
    tag.textContent = burn.player;
    if (burn.via !== 'base cost') {
      var via = document.createElement('em');
      via.textContent = ' ' + burn.via;
      tag.appendChild(via);
    }
    cell.appendChild(tag);
  }

  function render(data) {
    clearBoard();
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

    if (ids.length === 0) {
      clearBoard();
      resultBox.hidden = true;
      return;
    }

    fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken
      },
      credentials: 'same-origin',           // send the session + csrf cookies
      body: JSON.stringify({ entry_ids: ids })
    })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error(data.error || 'Request failed.');
          return data;
        });
      })
      .then(render)
      .catch(function (err) { showError(err.message); });
  }

  boxes.forEach(function (box) { box.addEventListener('change', preview); });
  enforceMax();
})();
