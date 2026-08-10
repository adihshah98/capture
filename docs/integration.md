# Adding session recording to your site

Replace `https://capture.example.com` with the server address you were given
and `cap_YOURTOKEN` with your project token throughout.

## The integration

Paste this on every page you want recorded — `<head>` or `<body>`, either
works:

```html
<script>
(function () {
  var host = 'https://capture.example.com';
  window.CAPTURE_CONFIG = Object.assign(
    { endpoint: host + '/ingest', configUrl: host + '/config', token: 'cap_YOURTOKEN' },
    window.CAPTURE_CONFIG || {}
  );
  function load() {
    if (window.__captureLoaded) return;
    window.__captureLoaded = 1;
    var s = document.createElement('script');
    s.async = 1;
    s.src = host + '/recorder.bundle.js';
    s.onerror = function () { window.__captureLoaded = 0; };
    document.head.appendChild(s);
  }
  function schedule() {
    if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 3000 });
    else setTimeout(load, 1500);
  }
  if (document.readyState === 'complete') schedule();
  else addEventListener('load', schedule);
})();
</script>
```

**Nothing is fetched while your page loads.** The snippet waits for `load`,
then for the browser to go idle, then pulls the recorder in the background.
If our servers are slow or down, your page behaves as if the snippet weren't
there and you simply get no recording for that visit.

No account, no build step, no cookies. Works on static sites, SPAs and
server-rendered apps; single-page navigations need no extra wiring.

See [Google Tag Manager](#google-tag-manager), [Shopify](#shopify) or
[CSP](#content-security-policy-csp) if any apply to you.

### If you can't use an inline script

Under a strict CSP, or in a tag manager that only accepts a URL:

```html
<script async src="https://capture.example.com/capture.js" data-token="cap_YOURTOKEN"></script>
```

`async` is required — without it the browser stops parsing your page until
the file arrives. This form is one step weaker than the snippet: a pending
request still holds back your `window.load` event, so prefer the snippet if
you have `onload` handlers.

## What it costs your page

- Nothing during page load. The recorder (~50 KB gzipped) arrives once your
  page is complete and the browser is idle. Core Web Vitals untouched.
- Recordings are batched and gzipped; a typical session is a few hundred KB.
- The recorder is cached for an hour, so repeat visits usually fetch nothing.

## Privacy defaults

- **Input values are masked** to `*` before they leave the browser.
- `class="capture-block"` hides an element entirely; `class="capture-mask"`
  keeps its layout but masks its text.
- Can't edit the markup — a CMS, a third-party widget? Ask us to set
  `maskTextSelector` or `blockSelector` for your project. Any CSS selector,
  no page changes.
- **Sensitive URL parameters are redacted** before storage (`token`,
  `password`, `secret`, `auth`, `api_key`, `session`, `code`, `email`,
  `phone` and similar), in query strings and OAuth fragments alike. A
  password-reset or magic link is safe to record.
- **Console output and network requests are never captured** — DOM and
  interactions only.
- **Clicks are labelled with the element's visible text** (“Buy now”).
  Labels honour the masking rules, never come from text inputs, and anything
  resembling a card or phone number is scrubbed.
- **Return visits group by a random UUID** stored in the visitor's browser —
  never linked to accounts, emails or IP addresses, never shared across
  devices. Opting out or clearing site data removes it.

**Visible page text is recorded as-is** — that is what makes a replay a
replay. Mask anything that should never be stored.

## Consent

We ship no banner; consent is yours to collect.

**Simplest:** don't load the snippet until consent, by firing it from your
consent trigger (see [Google Tag Manager](#google-tag-manager)).

**If it's on every page,** set `requireConsent` in a block above the snippet
— anything you set beforehand is preserved:

```html
<script>window.CAPTURE_CONFIG = { requireConsent: true };</script>
<!-- the integration snippet goes here, unchanged -->
```

The recorder loads but writes nothing — no session id, no config cache, no
recording — until you call:

```js
window.capture.grantConsent();  // start recording
window.capture.optOut();        // stop, erase, stay off
window.capture.optIn();         // reverse an earlier opt-out
window.capture.isOptedOut();    // current state, for your banner
window.capture.status();        // 'awaiting-consent' | 'opted-out' | 'active' | …
```

`optOut()` is remembered across page loads via one key (`capture_opt_out`).
`requireConsent` must be set inline, not in server-side config — a remote
value would arrive after the storage write it exists to prevent.

**Global Privacy Control** is honoured automatically: those visitors are
never recorded. Set `respectGlobalPrivacyControl: false` to disable it. An
explicit `grantConsent()` still wins.

## Google Tag Manager

1. **Tags → New → Custom HTML**, and paste the snippet from
   [The integration](#the-integration).
2. Trigger: **All Pages**. The snippet defers everything to page idle, so
   the firing point makes no practical difference.
3. Preview, then publish.

- Double-firing is harmless — the snippet guards itself with
  `window.__captureLoaded`.
- For consent-gated recording, use your consent trigger instead of All Pages.
- CSP rules still apply; GTM only changes how the snippet is delivered.

## Shopify

The same snippet either way.

**Route 1 — theme code editor.** **Online Store → Themes → ⋯ → Edit code**,
open `layout/theme.liquid`, paste the snippet before `</head>`, Save.

**Route 2 — Custom Liquid block**, if code editing is unavailable to your
account or the editor renders blank. **Online Store → Themes → Customize**,
find the **Footer** section group, **Add section → Custom liquid**, paste
the snippet, Save.

If your admin has **Sidekick**, you can instead tell it: *"Add a Custom
liquid block to the Footer section group so it renders on every page,
containing exactly this code:"* followed by the snippet — then review the
block it creates and Save. This is how we validated the integration
ourselves.

Both routes cover every storefront page including cart and search. Shopify's
checkout runs no theme code, so checkout itself is never recorded.

- **The snippet lives in the theme.** Switching or republishing a theme
  drops it — re-add it after theme changes, and add it to any unpublished
  theme you preview-test.
- The theme editor's preview runs the real theme, so sessions you record
  while customising will appear in your recordings.
- Password-protected and trial stores work fine.
- Verify with `window.capture.status()` on the storefront (see
  [Verifying the integration](#verifying-the-integration)).

### Linking purchases to recordings

Recording stops at checkout, but a Shopify
[custom pixel](https://help.shopify.com/en/manual/promoting-marketing/pixels/custom-pixels)
does run there and can link the outcome back. Sessions then carry a green
**Purchased** badge (order id and total) or an amber **Reached checkout**.

1. **Settings → Customer events → Add custom pixel**, named e.g.
   `Capture purchases`.
2. Paste this, with the same two values as the storefront snippet:

   ```js
   const CAPTURE_SERVER = 'https://capture.example.com';
   const CAPTURE_TOKEN = 'cap_YOURTOKEN';

   async function sendToCapture(event, fields) {
     try {
       // The recorder keeps its session id in localStorage; the pixel
       // sandbox reads the storefront's storage through this async API.
       const raw = await browser.localStorage.getItem('capture_session');
       const session = raw && JSON.parse(raw);
       if (!session || !session.id) return; // visitor wasn't recorded
       await fetch(`${CAPTURE_SERVER}/order?session_id=${session.id}&token=${CAPTURE_TOKEN}`, {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify({ event, ...fields }),
         keepalive: true,
       });
     } catch (e) {
       // Never let tracking interfere with checkout.
     }
   }

   analytics.subscribe('checkout_started', () => {
     sendToCapture('checkout_started', {});
   });

   analytics.subscribe('checkout_completed', (event) => {
     const checkout = event.data.checkout;
     sendToCapture('checkout_completed', {
       order_id: checkout.order && checkout.order.id,
       total: checkout.totalPrice && checkout.totalPrice.amount,
       currency: checkout.totalPrice && checkout.totalPrice.currencyCode,
     });
   });
   ```

3. **Save**, then **Connect**. Verify with a test order — that session
   should show a green **Purchased** badge.

- The checkout itself is still not recorded; this links the outcome, which
  is a Shopify restriction no script tag can lift.
- Only session id, order id, total and currency are sent — no customer
  details.
- **Storefront and checkout must share a domain** (they do by default). A
  purchase completed entirely inside the Shop app can't be linked.
- Sampled-out and opted-out visitors have no session id, so nothing is sent.
- Checkouts started but never completed keep the amber badge — usually the
  recordings worth watching.

## Content-Security-Policy (CSP)

Recording needs exactly two directives:

| Directive | Why |
|---|---|
| `script-src` | Runs the snippet and the recorder it injects |
| `connect-src` | Background `fetch()` calls that deliver the recordings |

```
Content-Security-Policy:
  script-src  'self' https://capture.example.com;
  connect-src 'self' https://capture.example.com;
```

Nothing else — the recorder loads no styles, images, fonts or workers.

**Nonce-based policies.** The snippet is inline and injects a script, so
nonce-only policies block both:

- **With `'strict-dynamic'`** (recommended): nonce the inline snippet and
  the injected recorder inherits its trust.
- **Without it:** nonce the snippet and keep the capture origin in
  `script-src`.
- **If inline scripts are forbidden outright:** use the
  [one-line form](#if-you-cant-use-an-inline-script) and nonce that instead.

**Symptoms.** Missing `script-src` → `Refused to load the script
'…/recorder.bundle.js'` and nothing records. Missing `connect-src` → the
recorder loads but every send fails with `Refused to connect to '…/ingest'`.

Validate with a `Content-Security-Policy-Report-Only` header first.

## Verifying the integration

```js
window.capture.status()   // "active" (or "buffering" with trigger-based recording)
window.capture.config()   // the effective settings, for debugging
```

Click around for half a minute; your session should appear in the replay UI.
`"disabled"` means the session was sampled out by design — a later visit
re-rolls.

## Marking interesting moments

With trigger-based recording configured, flag a session as worth keeping
from your own code — an error handler, a "report a bug" button:

```js
window.capture.trigger('user-reported-bug');
```

The recording, including the buffered lead-up, is then sent.
