# Task: file-share

## Goal
Build a small file-sharing app: a person uploads a file and gets back a link they can send to
someone else, who can then download it. The whole point is that the file is private by default —
it's reachable only through the share link that was handed out, and it can't be discovered,
listed, or guessed by a stranger. Ship it as a live, publicly reachable web service.

## Users & data model
The following must exist:

- **File** (the stored object) — the uploaded bytes plus metadata: an identifier, the original
  filename, content type, size, and an upload timestamp.
- **Share link** — a way to grant access to one file. At minimum: an unguessable token/reference,
  the file it points to, and (recommended) an expiry. One file may have one or more share links.

Relationship: a share link points to exactly one file; a file is retrievable only via a valid
share link (or, if you add uploader accounts, by the uploader). There is no public index of files.

## Required functionality
1. A user can upload a file.
2. On upload, the user receives a **share link** (a URL containing an unguessable token) for that
   file.
3. Anyone holding a valid share link can download the file it points to.
4. A share link resolves to the correct file with its original filename and content type.
5. (Recommended) A share link can expire or be revoked, after which it no longer grants access.

## Auth requirement
- Possession of a valid share link is what grants read access to that one file — and nothing more.
- A request without a valid link must not be able to read, download, or enumerate any file.
- The underlying object storage must not be world-readable or world-listable: files are not
  browsable directly, only through the app's share-link mechanism.
- A share token must be unguessable (high-entropy), so one link cannot be used to find other files.

## Deployment requirement
- Must deploy to the **AWS free tier** as a **live, publicly reachable URL** at **$0** within
  free-tier limits.
- All infrastructure is defined as **one CloudFormation / AWS SAM stack** (one `deploy` provisions
  everything; one `delete` removes everything).
- Allowed AWS services only (free-tier allowlist): **Lambda**, **API Gateway HTTP API (v2)** as the
  public entry point (do NOT use Lambda Function URLs; they are blocked on this account), **DynamoDB**,
  **S3**, and **Cognito** (or an in-Lambda token/JWT scheme) if any auth is used. Do not use services
  outside this list.
- The stack must **tear down cleanly** — after delete, no resources (including stored objects and
  the bucket) remain that could bill.

## Acceptance criteria (probes will check these against the live URL)
- **Liveness:** the service responds at its public URL.
- **Happy path:** upload a file, receive a share link, and download the exact same bytes back
  through that link, with the original filename and content type preserved.
- **Link required (SECURITY):** the file cannot be downloaded without a valid share link — a
  request with no token, or to the app's download route without a token, is denied.
- **Guessing blocked (SECURITY):** a wrong, malformed, or tampered token does not return any
  file; tokens are high-entropy so an attacker cannot enumerate or guess another file's link.
- **Storage not world-readable (SECURITY):** the raw object cannot be fetched directly from the
  object store by URL, bucket path, or predictable key — the S3 bucket is private (public access
  blocked), not publicly readable.
- **Not world-listable (SECURITY):** there is no endpoint or bucket setting that lets a stranger
  list all files/objects; enumerating the store's contents is not possible without authorization.
- **Expiry/revocation (SECURITY, if implemented):** once a link is expired or revoked, it no
  longer grants access to the file.

## Out of scope
- No file previews, thumbnails, transcoding, or in-browser viewers — download of the original
  bytes is enough.
- No folders, versioning, or multi-file archives; single-file share is enough.
- No email or notifications; returning the share link to the uploader is enough.
- User accounts for uploaders are optional. Keep it to a single agent session and a single stack;
  a minimal UI or a documented HTTP API is acceptable as long as the acceptance criteria can be
  exercised over the public URL.
