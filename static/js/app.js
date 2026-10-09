// Site behaviour that used to live in inline handlers (kept out of HTML so the CSP can forbid
// inline script). Forms with data-confirm="…" ask before submitting.
document.addEventListener("submit", function (e) {
  var form = e.target;
  if (form && form.matches && form.matches("form[data-confirm]")) {
    if (!window.confirm(form.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  }
}, true);
