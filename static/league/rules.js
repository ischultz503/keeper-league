/* "Save as PDF" on the rules page.
 *
 * There is no server-side PDF here on purpose. Every library that renders one
 * (WeasyPrint and friends) drags in system libraries that would have to be
 * installed in the Docker image and on a Windows dev machine, to produce a
 * worse copy of something the browser already does perfectly -- and it would be
 * a SECOND renderer to keep looking like the page.
 *
 * So this opens the print dialog, where "Save as PDF" is a destination on every
 * desktop browser, and Chrome and Safari on a phone both offer it too. The
 * @media print block in style.css is what makes the output worth saving; the
 * page title is what names the file.
 */
(function () {
  'use strict';

  var button = document.getElementById('print-rules');
  if (!button) return;                      // not the rules page

  // Rendered hidden, revealed here: without JavaScript this button could not
  // do anything, and a dead control is worse than no control.
  button.hidden = false;

  button.addEventListener('click', function () {
    window.print();
  });
})();
