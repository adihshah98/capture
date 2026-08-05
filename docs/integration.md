# Adding session recording to your site

This guide is for the team integrating a capture tag on their website.
Everywhere below, replace `https://capture.example.com` with the capture
server address you were given, and `cap_YOURTOKEN` with your project token.

## The integration

Add this line anywhere in your site's HTML (ideally near the end of
`<body>`), on every page you want recorded:

```html
<script src="https://capture.example.com/capture.js" data-token="cap_YOURTOKEN"></script>
```

That's the entire integration — no account, no build step, no cookies to
configure. The tag works on plain HTML sites, SPAs (React, Vue, …), and
server-rendered apps alike; single-page navigations are tracked without any
extra wiring.

If you deploy tags through Google Tag Manager instead of editing HTML, see
[Google Tag Manager](#google-tag-manager) below. If your store runs on
Shopify, see [Shopify](#shopify). If your site enforces a
Content-Security-Policy, see [Content-Security-Policy](#content-security-policy-csp).

## What it costs your page

- The tag is **~1 KB** and does nothing until your page has finished
  loading and the browser is idle; only then does it lazy-load the actual
  recorder. It never competes with your page load or your Core Web Vitals.
- Recordings are batched, gzipped, and sent in the background every few
  seconds; a typical session is a few hundred KB total.

## Privacy defaults

- **Everything users type into inputs is masked** to `*` before it leaves
  their browser — raw form values are never transmitted or stored.
- To exclude an element from recordings entirely (it appears as an empty
  box), add `class="capture-block"`.
- To keep an element's layout but mask its text content, add
  `class="capture-mask"`.
- When you can't edit the markup — a CMS, a third-party widget — ask us to
  set `maskTextSelector` or `blockSelector` for your project instead. They
  take any CSS selector and need no page changes.
- **Secrets in URLs are stripped before anything is stored.** Parameter
  values whose names look sensitive (`token`, `password`, `secret`, `auth`,
  `api_key`, `session`, `code`, `email`, `phone`, and similar) are replaced
  with `REDACTED` in every recorded page URL, query string and OAuth
  fragment both. So a password-reset or magic link is safe to record; the
  rest of the URL is kept intact.
- **Console output and network requests are not captured at all.** The
  recording is the DOM and user interactions only.

Note that **visible page text is recorded as-is** — that is what makes a
replay a replay. If a page renders something that should never be stored,
mask it with one of the options above.

## Consent

Capture ships no banner of its own — consent is yours to collect, and you
almost certainly already do. There are two ways to wire it up.

**Simplest: don't load the tag until consent.** Fire it from your consent
trigger rather than on all pages (see [Google Tag Manager](#google-tag-manager)
below). Nothing runs, so nothing is stored.

**If the tag loads on every page** and consent arrives later, set
`requireConsent` and drive it from your consent platform:

```html
<script>window.CAPTURE_CONFIG = { requireConsent: true };</script>
<script src="https://capture.example.com/capture.js" data-token="cap_xxx"></script>
```

The bundle loads but writes **nothing** — no session id, no config cache, no
recording — until you call:

```js
window.capture.grantConsent();  // consent given: start recording
window.capture.optOut();        // refused or withdrawn: stop, erase, stay off
window.capture.optIn();         // reverse an earlier opt-out
window.capture.isOptedOut();    // current state, for your banner's UI
window.capture.status();        // 'awaiting-consent' | 'opted-out' | 'active' | …
```

`optOut()` stops recording immediately, deletes the session and window ids
and the cached config, and is remembered on later page loads. Remembering it
does mean storing one key (`capture_opt_out`); the alternative is asking on
every page load.

`requireConsent` must be set inline in the page, not in your project's
server-side config — a remote value would arrive after the storage write it
exists to prevent.

**Global Privacy Control** (`navigator.globalPrivacyControl`, a
legally recognised opt-out in several US states) is honoured automatically:
those visitors are never recorded and nothing is stored for them. Set
`respectGlobalPrivacyControl: false` to turn that off. An explicit
`grantConsent()` still wins, on the basis that your banner asked directly.

## Google Tag Manager

1. In your container: **Tags → New → Custom HTML**.
2. Paste the tag as the HTML content:

   ```html
   <script src="https://capture.example.com/capture.js" data-token="cap_YOURTOKEN"></script>
   ```

3. Set the trigger to **All Pages** (the standard Page View trigger is
   fine — the snippet defers all real work until the page is idle, so an
   earlier or later firing point makes no practical difference).
4. Preview, then publish the container.

Notes:

- **One firing per page is enough.** The snippet guards itself
  (`window.__captureLoaded`), so an accidental double-fire is harmless.
- **Consent:** to record only after cookie/tracking consent, attach your
  consent trigger (e.g. a custom event trigger fired by your consent
  banner) instead of All Pages. Recording starts on the page view where
  the tag first fires.
- **CSP still applies.** GTM only changes how the tag is delivered; if
  your site enforces a CSP, the directives below are still required.

## Shopify

Shopify themes can't be edited as plain HTML files, but the integration is
the same single tag either way. Two routes, in order of preference:

**Route 1 — theme code editor** (starts the recorder earliest):

1. In the Shopify admin: **Online Store → Themes →** your live theme's
   **⋯ menu → Edit code**.
2. Open `layout/theme.liquid` and paste the tag just before `</head>`:

   ```html
   <script src="https://capture.example.com/capture.js" data-token="cap_YOURTOKEN"></script>
   ```

3. Save. The tag is now on every page of the storefront.

**Route 2 — Custom Liquid block** (no code editor needed; use this if code
editing is unavailable to your account, or the code editor won't load —
we've seen it render blank on some stores):

1. **Online Store → Themes → Customize** to open the theme editor.
2. In the left sidebar, find the **Footer** section group (it renders on
   every page) and click **Add section** (or **Add block**, depending on
   theme) **→ Custom liquid**.
3. Paste the same tag into the block's "Liquid code" field, verify the
   token matches what you were given, and click **Save**.

   If your admin has **Sidekick** (Shopify's assistant in the editor), you
   can alternatively paste it this instruction verbatim: *"Add a Custom
   liquid block to the Footer section group so it renders on every page,
   containing exactly this code: `<script src="…/capture.js"
   data-token="cap_…"></script>`"* — then review the block it creates and
   Save. This is how we validated the integration ourselves.

Both routes cover every storefront page, including cart and search;
Shopify's checkout pages don't run theme code, so checkout itself is not
recorded. Coverage is identical between the two — the footer placement just
starts the recorder a moment later, which makes no practical difference
since the snippet already waits for page idle.

Notes:

- **The tag lives in the theme.** Switching or republishing a different
  theme drops it (true of any script tag on Shopify, analytics included) —
  re-add it after a theme change, and add it to any unpublished theme you
  preview-test.
- **The theme editor's preview runs the real theme**, so sessions recorded
  while you customize the theme may appear in your recordings — they're
  real recordings of you, not a bug.
- **Password-protected or trial stores work fine**; the recorder doesn't
  care how the visitor got in.
- Verify the same way as any site: open the storefront and check
  `window.capture.status()` returns `"active"` (see
  [Verifying the integration](#verifying-the-integration)).

### Did they buy? Linking purchases to recordings

The recording ends when a visitor enters checkout — Shopify runs no theme
code there. But Shopify **does** let a [custom pixel](https://help.shopify.com/en/manual/promoting-marketing/pixels/custom-pixels)
run on checkout pages, including the thank-you page, so the *outcome* can be
linked back to the recorded session. Sessions in the replay UI then carry a
green **Purchased** badge (with order id and total) or an amber **Reached
checkout** badge.

1. In the Shopify admin: **Settings → Customer events → Add custom pixel**,
   name it e.g. `Capture purchases`.
2. Paste this as the pixel code, with your server address and token filled
   in (the same two values as the storefront tag):

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

3. Click **Save**, then **Connect**. Verify with a test order: the session
   you placed it from shows a green **Purchased** badge in the session list.

What to know:

- **The checkout itself is still not recorded** — that's a Shopify
  restriction no script tag can lift. This links the purchase (or the
  drop-off) to the session so you know how the story ended.
- Only the capture session id, order id, total, and currency are sent —
  no customer details.
- **Storefront and checkout must share a domain** (they do by default).
  A purchase completed off-domain — e.g. entirely inside the Shop app —
  can't see the storefront's session id and won't be linked.
- Visitors who were sampled out or opted out have no session id in
  storage, so the pixel sends nothing for them — consent choices carry
  over automatically.
- A session whose visitor started checkout but never completed it keeps
  the amber **Reached checkout** badge — those are your checkout-abandonment
  recordings, usually the ones worth watching.

## Content-Security-Policy (CSP)

If your site sends a `Content-Security-Policy` header, the browser will
block the snippet unless the capture server is allowed. Recording needs
exactly two things:

| Directive | Why |
|---|---|
| `script-src` | Loads `capture.js` and the lazy-loaded recorder bundle |
| `connect-src` | Background `fetch()` calls that deliver the recordings |

Add the capture origin to both:

```
Content-Security-Policy:
  script-src  'self' https://capture.example.com;
  connect-src 'self' https://capture.example.com;
```

Nothing else is required — the recorder loads no styles, images, fonts, or
workers from the capture server.

### Nonce-based policies

The snippet injects the recorder bundle with a dynamically created
`<script>` element, which nonce-only policies block:

- **With `'strict-dynamic'`** (recommended): put your nonce on the snippet
  tag and the injected recorder inherits its trust automatically.

  ```html
  <script nonce="{{nonce}}" src="https://capture.example.com/capture.js"
          data-token="cap_YOURTOKEN"></script>
  ```

- **Without `'strict-dynamic'`**: keep `https://capture.example.com` in the
  `script-src` allowlist (as above) alongside your nonces.

### Symptoms of a missing directive

- `script-src` missing → console shows `Refused to load the script
  'https://capture.example.com/capture.js'…` and nothing records.
- `connect-src` missing → the recorder loads but every send fails with
  `Refused to connect to 'https://capture.example.com/ingest'…`.

To validate without breaking your site, ship the change in a
`Content-Security-Policy-Report-Only` header first and watch for reports,
then promote it to the enforcing header.

## Verifying the integration

Open your site and run in the browser console:

```js
window.capture.status()   // "active" (or "buffering" if trigger-based recording is configured)
window.capture.config()   // the effective settings, for debugging
```

Then click around for half a minute — your session should appear in the
operator's replay UI. If `status()` is `"disabled"`, the session was
sampled out (by design when a sample rate is configured); a later visit
will re-roll.

## Advanced: marking interesting moments

If trigger-based recording is configured for your project, you can flag a
session as worth keeping from your own code — for example in an error
handler or a "report a bug" button:

```js
window.capture.trigger('user-reported-bug');
```

The recording, including the buffered lead-up to that moment, is then sent.
