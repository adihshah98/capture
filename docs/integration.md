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
[Google Tag Manager](#google-tag-manager) below. If your site enforces a
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
- Network capture records URLs, methods, status codes, and timings only.
  Request/response headers and bodies are not captured, and sensitive
  headers (authorization, cookies) are always redacted.

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
