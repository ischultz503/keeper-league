/* The draft-time poll grid.
 *
 * Two jobs, in the same style as board.js -- no framework, no build step:
 *
 *   Voting   -- clicking a cell in your own row POSTs to /api/draft-poll/vote/
 *               and paints the answer straight away. Clicking the answer you
 *               already have clears it; the server decides that, this file only
 *               guesses at it so the button reacts instantly.
 *   Polling  -- every fifteen seconds, ONLY while the tab is visible, ask the
 *               server whether anything has moved since the stamp we hold. The
 *               usual answer is {"changed": false} and costs two aggregates.
 *
 * Everything this file needs -- the endpoints, the stamp, my team -- is read
 * off data- attributes on #poll-grid, and the answer glyphs come from a
 * json_script block the server renders. No URLs and no answer values are
 * written down here, so nothing in this file can drift out of step with Python.
 */
(function () {
  'use strict';

  var grid = document.getElementById('poll-grid');
  if (!grid) return;                        // not the poll page, or no options

  var voteUrl = grid.dataset.voteUrl;
  var stateUrl = grid.dataset.stateUrl;
  var ownTeam = grid.dataset.ownTeam || '';
  var closed = grid.dataset.closed === '1';

  var errorBox = document.getElementById('poll-error');
  var altList = document.getElementById('poll-alt-list');

  var glyphs = readJson('poll-glyphs') || {};
  var labels = readJson('poll-labels') || {};

  /* Django's CSRF check is a double-submit: the server compares this header
   * against the csrftoken cookie. fetch() sends the cookie by itself but knows
   * nothing about the token, so we read the one {% csrf_token %} rendered. */
  var tokenField = grid.querySelector('[name=csrfmiddlewaretoken]');
  var csrfToken = tokenField ? tokenField.value : '';

  var stamp = grid.dataset.stamp || '';
  var timer = null;
  var failures = 0;

  /* Option ids with a save outstanding. A repaint SKIPS these cells: a state
   * response that left the server before my click landed would otherwise paint
   * the old answer back over the button I just pressed, and it would look like
   * the click had been rejected. */
  var saving = {};

  var POLL_MS = 15000;
  var MAX_FAILURES = 3;

  function readJson(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (problem) {
      return null;
    }
  }

  function showError(message) {
    if (!errorBox) return;
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function clearError() {
    if (!errorBox) return;
    errorBox.hidden = true;
    errorBox.textContent = '';
  }

  function request(url, options) {
    return fetch(url, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || 'Request failed.');
        return data;
      });
    });
  }

  /* --- Painting ----------------------------------------------------------- */

  function cellFor(optionId, teamId) {
    return grid.querySelector(
      '.poll-cell[data-option-id="' + optionId + '"][data-team-id="' + teamId + '"]'
    );
  }

  function paintOwnCell(cell, answer) {
    Array.prototype.forEach.call(cell.querySelectorAll('.poll-btn'), function (button) {
      var on = button.dataset.answer === answer;
      button.classList.toggle('on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function paintMarkCell(cell, answer, owner) {
    var mark = cell.querySelector('.poll-mark');
    if (!mark) return;

    mark.className = 'poll-mark ans-' + (answer || 'none');
    // The glyph is what carries the meaning -- the colour only reinforces it,
    // which is why it is repainted here and not left to CSS.
    mark.firstElementChild.textContent = glyphs[answer] || '·';

    var label = labels[answer] || 'no answer yet';
    mark.lastElementChild.textContent = label;
    if (owner) mark.title = owner + ': ' + label;
  }

  function paintCell(optionId, teamId, answer, owner) {
    var cell = cellFor(optionId, teamId);
    if (!cell) return;

    if (cell.querySelector('.poll-choice')) {
      paintOwnCell(cell, answer);
    } else {
      paintMarkCell(cell, answer, owner);
    }
  }

  function paintTotals(options) {
    options.forEach(function (option) {
      var selector = '[data-option-id="' + option.id + '"]';

      Array.prototype.forEach.call(
        grid.querySelectorAll('.poll-count' + selector),
        function (node) {
          node.textContent = node.dataset.count === 'yes' ? option.yes : option.available;
          node.classList.toggle('best', !!option.best);
        }
      );

      var head = grid.querySelector('.poll-head' + selector);
      if (head) head.classList.toggle('best', !!option.best);
    });
  }

  function paintAlternatives(notes) {
    // Only into a list that is already on the page. A team's first note is a
    // form POST and a full reload for whoever wrote it, so the list appears
    // there; building one from scratch here would duplicate the template for a
    // case that lasts one page load.
    if (!altList) return;

    altList.textContent = '';
    notes.forEach(function (note) {
      var item = document.createElement('li');
      item.className = 'feedback-item';

      var who = document.createElement('p');
      who.className = 'feedback-meta';
      var name = document.createElement('strong');
      name.textContent = note.team;
      who.appendChild(name);

      var body = document.createElement('div');
      body.className = 'feedback-body';
      // textContent, never innerHTML: this is somebody else's typing.
      body.textContent = note.text;

      item.appendChild(who);
      item.appendChild(body);
      altList.appendChild(item);
    });
  }

  function repaint(state) {
    var owners = {};
    (state.teams || []).forEach(function (team) { owners[team.id] = team.owner; });

    (state.options || []).forEach(function (option) {
      if (saving[option.id]) return;        // a click of mine is still in the air
      var answers = (state.answers || {})[String(option.id)] || {};

      (state.teams || []).forEach(function (team) {
        paintCell(option.id, team.id, answers[String(team.id)] || '', owners[team.id]);
      });
    });

    paintTotals((state.options || []).filter(function (option) {
      return !saving[option.id];
    }));
    paintAlternatives(state.alternatives || []);
  }

  /* --- Voting ------------------------------------------------------------- */

  if (ownTeam && !closed) {
    // One delegated listener on the grid rather than thirty on the buttons:
    // the cells are repainted in place, but the buttons themselves are never
    // replaced, so either would work -- this is simply less to keep track of.
    grid.addEventListener('click', function (event) {
      var button = event.target.closest('.poll-btn');
      if (!button) return;

      var cell = button.closest('.poll-cell');
      var optionId = cell.dataset.optionId;
      var answer = button.dataset.answer;

      // Same rule the server applies: clicking the answer you already have
      // clears it. Guessed here only so the button reacts on the click; the
      // response below is what settles it.
      var wanted = button.classList.contains('on') ? '' : answer;
      paintOwnCell(cell, wanted);

      saving[optionId] = true;
      request(voteUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken
        },
        credentials: 'same-origin',
        body: JSON.stringify({ option_id: parseInt(optionId, 10), answer: answer })
      }).then(function (data) {
        delete saving[optionId];
        paintOwnCell(cell, data.answer || '');
        paintTotals(data.options || []);
        stamp = data.stamp || stamp;
        clearError();
      }).catch(function (problem) {
        delete saving[optionId];
        showError(problem.message + ' Your answer was not saved.');
        // Throw away the stamp so the next poll returns the full grid and
        // paints over the guess above, rather than leaving the button showing
        // an answer the server never took.
        stamp = '';
        check();
      });
    });
  }

  /* --- Polling ------------------------------------------------------------ */

  function check() {
    return request(stateUrl + '?since=' + encodeURIComponent(stamp), {
      credentials: 'same-origin'
    }).then(function (data) {
      failures = 0;
      clearError();
      stamp = data.stamp || stamp;
      if (data.changed) repaint(data);
      // A check that succeeds after the timer was torn down (the tab came back
      // and the server is answering again) has to put it back, or the grid
      // would sit there live-looking and never update again.
      start();
    }).catch(function (problem) {
      failures += 1;
      if (failures >= MAX_FAILURES) {
        stop();
        showError(
          'Cannot reach the server, so this grid has stopped updating. '
          + 'Reload the page to try again.'
        );
      } else {
        // Silent staleness on a scheduling page is worse than an honest error.
        showError('Trouble reaching the server: ' + problem.message);
      }
    });
  }

  function start() {
    if (timer === null && document.visibilityState === 'visible' && failures < MAX_FAILURES) {
      timer = setInterval(check, POLL_MS);
    }
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /* Ten laptops left open overnight should generate no traffic at all. A hidden
   * tab has nobody looking at it, so there is nothing for a refresh to be in
   * time for -- the timer is torn down entirely rather than left ticking, and a
   * returning tab checks once immediately so it is never fifteen seconds stale
   * at the moment someone looks back at it. */
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      check();
      start();
    } else {
      stop();
    }
  });

  start();
})();
