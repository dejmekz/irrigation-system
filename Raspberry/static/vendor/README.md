# Vendored front-end assets

These are served locally rather than from a CDN. The controller runs on a LAN
that may have no internet access, and a CDN failure is not cosmetic: without
`socket.io.min.js` the `io()` call in `base.html` throws, which kills the MQTT
status badge, live valve state and every script that follows on the page.

| File | Version | Source |
|---|---|---|
| `bootstrap.min.css` | 5.3.3 | `cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css` |
| `bootstrap.bundle.min.js` | 5.3.3 | `cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js` |
| `bootstrap-icons.min.css` | 1.11.3 | `cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css` |
| `fonts/bootstrap-icons.woff2` | 1.11.3 | `cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff2` |
| `fonts/bootstrap-icons.woff` | 1.11.3 | `cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/fonts/bootstrap-icons.woff` |
| `socket.io.min.js` | 4.7.5 | `cdn.socket.io/4.7.5/socket.io.min.js` |

Notes:

- `fonts/` must stay a sibling of `bootstrap-icons.min.css` — the stylesheet
  references `url("fonts/bootstrap-icons.woff2?…")` relative to itself.
- The socket.io **client major version must match the server**. 4.x pairs with
  the Socket.IO 5 / Engine.IO 4 protocol that `flask-socketio>=5.3.6` speaks.
  Do not bump it independently of `requirements.txt`.
- Files are unmodified upstream builds. To refresh, re-download from the URLs
  above and bump the versions in this table.
