# Password reset and resume upload

## Password reset

1. `PUT /v1/auth/me/email` sets the address for an existing account (current password required).
   New accounts can pass `email` at signup. Addresses are lower-cased and unique. They are
   not verified by a confirmation email.
2. `POST /v1/auth/password-reset/request` with `{"email": ...}` always answers `202` with the same
   text, whether or not the address exists. It is limited per IP (`AUTH_RESET_IP_LIMIT`) and per address.
3. The email holds a link `PASSWORD_RESET_URL?token=...`. The token is random, stored only as a hash,
   works once, and expires after `PASSWORD_RESET_MINUTES` (default 30). A new request voids older links.
4. `POST /v1/auth/password-reset/confirm` with `{"token", "new_password"}` sets the password and signs
   out every session (all refresh tokens are revoked). Bad, used or expired tokens return `400`.

The API only builds the link. Your front end needs a page at `PASSWORD_RESET_URL` that reads
`token` and calls the confirm endpoint.

### Email backends (`EMAIL_BACKEND`)

| Value | Behavior |
|---|---|
| `disabled` (default) | The request endpoint returns `503`. |
| `console` | Writes the whole message, link included, to the log. Development only. |
| `smtp` | Sends through `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` with STARTTLS. Not tested against a real server. |

To add another provider, implement `send(to, subject, body)` and return it from
`get_email_sender` in `workwise/core/mailer.py`. Delivery runs after the response, and a failure is
logged without the message text.

Old reset tokens can be purged with `python -m workwise.operations.retention password-resets`.

## Resume upload

- `PUT /v1/resume` (multipart field `file`) stores one PDF per user and replaces the previous one.
  Limits: PDF only (checked by `%PDF-` start and `%%EOF` end, not by file name), at most
  `RESUME_MAX_BYTES` (default 2 MB). The file is not scanned for malware or active content.
- `GET /v1/resume` downloads your resume. `DELETE /v1/resume` removes it.
- `GET /v1/applications/{id}/resume` lets the applicant, the job's company owner and admins download
  the applicant's current resume. Everyone else gets `404`.
- Files are saved in `RESUME_DIR` under a random name. The uploaded file name is never used.
  Downloads are sent as attachments with `Cache-Control: private, no-store`.

Storage is local disk. Docker Compose mounts the `resume_data` volume. Two API instances share it
only on one host. On Render's free plan the disk is wiped on every deploy and restart, so resumes
do not last there; use a persistent disk or object storage before relying on it.
