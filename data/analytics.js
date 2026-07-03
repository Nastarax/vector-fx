// Vector site analytics — single source of truth.
// Every page loads this one file (<script defer src="analytics.js">), so the
// tracking config lives here and nowhere else.
//
// SETUP (one-time, ~2 min):
//   1. Sign up free at https://www.goatcounter.com/signup and pick a code,
//      e.g. "vector-fx". Your dashboard is then https://vector-fx.goatcounter.com
//   2. Put that code in GOATCOUNTER_CODE below (replace YOUR_CODE_HERE).
//   3. Commit + push. Pageviews then show up in your GoatCounter dashboard.
//
// Until a real code is set this file no-ops: no requests, no console errors,
// so it is safe to ship the site publicly before you finish signup.
//
// To switch providers later (e.g. Cloudflare Web Analytics), just replace the
// body of this file. No page templates need to change.

(function () {
  var GOATCOUNTER_CODE = "YOUR_CODE_HERE"; // <-- set this, e.g. "vector-fx"

  if (!GOATCOUNTER_CODE || GOATCOUNTER_CODE === "YOUR_CODE_HERE") {
    return; // not configured yet
  }

  var s = document.createElement("script");
  s.async = true;
  s.src = "//gc.zgo.at/count.js";
  s.setAttribute("data-goatcounter", "https://" + GOATCOUNTER_CODE + ".goatcounter.com/count");
  document.head.appendChild(s);
})();
