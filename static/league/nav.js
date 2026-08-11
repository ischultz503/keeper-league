/* The header menu's two missing behaviours.
 *
 * <details> already gives us everything else -- the toggle, the button role,
 * Enter/Space activation, the screen reader's "expanded/collapsed" -- and it
 * gives it to us before any script has run. What it will NOT do is close
 * itself: an open <details> stays open until something sets .open = false.
 *
 * So this file is exactly two rules:
 *
 *   Escape          closes it and puts focus back on the button, because
 *                   otherwise focus is stranded inside a panel that no longer
 *                   exists on screen and the next Tab starts from nowhere.
 *   a click outside closes it, which is what everyone expects of a menu.
 *
 * Navigating away needs nothing: every link in the panel loads a new page, and
 * the new page's <details> starts closed.
 */
(function () {
  'use strict';

  var menu = document.getElementById('nav-menu');
  if (!menu) return;                        // logged out: there is no menu

  var toggle = menu.querySelector('summary');

  function close(refocus) {
    if (!menu.open) return;
    menu.open = false;
    // Only when the user asked for it (Escape). Stealing focus back on an
    // outside click would yank it away from whatever they just clicked.
    if (refocus && toggle) toggle.focus();
  }

  /* Both listeners go on `document`, NOT on `document.body`.
   *
   * <body> is an element with a box, and that box is only as tall as its
   * content. Click the empty space below a short page, or the page margin, or
   * the scrollbar, and the event target is <html> -- which is body's PARENT, so
   * the event never bubbles through body and a body-level listener never fires.
   * The menu would sit there open, and only on some parts of the page, which is
   * the most confusing kind of bug. `document` is the end of every event path,
   * so it hears the click wherever it landed.
   *
   * Same reasoning for keydown: focus can rest on <body> or on <html>, and only
   * `document` is guaranteed to be in the bubble path from either.
   */
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') close(true);
  });

  document.addEventListener('click', function (event) {
    if (!menu.open) return;
    // contains() covers the summary as well as the panel, so the click that
    // toggles the menu shut is not also closed by this handler -- which would
    // be a no-op today, but would fight the toggle if the order ever changed.
    if (menu.contains(event.target)) return;
    close(false);
  });
})();
