# Task: notes-auth

## Goal
Build a small multi-user notes app. Each person signs up, logs in, and keeps their own
private list of text notes. The whole point is privacy between accounts: my notes are mine,
and nobody else who uses the app can see or touch them. Ship it as a live, publicly reachable
web service.

## Users & data model
Two things must exist:

- **User** — a person with an account. At minimum: a unique identifier, a login handle
  (email or username), and whatever is needed to authenticate them. No two users share a handle.
- **Note** — a piece of text owned by exactly one user. At minimum: an identifier, the owning
  user, a title, a body, and created/updated timestamps.

Relationship: one user owns many notes; every note belongs to exactly one user. A note with no
owner is not valid.

## Required functionality
1. A new visitor can create an account.
2. A registered user can log in and receive a session/credential proving who they are.
3. A logged-in user can create a note (title + body).
4. A logged-in user can list their own notes.
5. A logged-in user can read one of their own notes by its id.
6. A logged-in user can edit one of their own notes.
7. A logged-in user can delete one of their own notes.

## Auth requirement
- Every note action (create, list, read, edit, delete) requires a valid authenticated identity.
- A user may only list, read, edit, or delete notes they own.
- Ownership is enforced on the server for every request. The client does not get to assert
  "I am user X" or "this note is mine" and be believed — the server decides from the credential.
- Requests with a missing, malformed, expired, or forged credential are rejected.

## Deployment requirement
- Must deploy to the **AWS free tier** as a **live, publicly reachable URL** at **$0** within
  free-tier limits.
- All infrastructure is defined as **one CloudFormation / AWS SAM stack** (one `deploy` provisions
  everything; one `delete` removes everything).
- Allowed AWS services only (free-tier allowlist): **Lambda**, **API Gateway HTTP API (v2)** as the
  public entry point (do NOT use Lambda Function URLs; they are blocked on this account), **DynamoDB**,
  **S3**, and **Cognito** (or an in-Lambda token/JWT scheme) for auth. Do not use services outside
  this list.
- The stack must **tear down cleanly** — after delete, no resources remain that could bill.

## Acceptance criteria (probes will check these against the live URL)
- **Liveness:** the service responds at its public URL.
- **Happy path:** a user can sign up, log in, create a note, then list and read that same note back.
- **Ownership on read/list:** when user A lists or reads notes, only A's notes are returned;
  A's list never contains B's notes.
- **Cross-tenant read blocked (SECURITY):** user A cannot read user B's note by its id — the
  server denies it (not found / forbidden), it does not return B's content.
- **Cross-tenant modify blocked (SECURITY):** user A cannot edit or delete user B's note — the
  attempt is denied and B's note is unchanged.
- **Auth required (SECURITY):** every note endpoint rejects requests that carry no credential.
- **Auth-spoof rejected (SECURITY):** a forged, tampered, or expired credential is rejected, not
  silently accepted as some valid user.
- **Server-side identity (SECURITY):** the owner of a created note is determined from the
  authenticated identity, not from a client-supplied user id — a client cannot create or reassign
  a note as another user.

## Out of scope
- No email verification, password reset, or any outbound email.
- No sharing, collaboration, roles/admin, or note-to-note links (notes are single-owner private).
- No rich text, attachments, search, or tagging — plain title + body text is enough.
- Keep it to a single agent session and a single stack; a minimal UI or a documented HTTP API is
  acceptable as long as the acceptance criteria can be exercised over the public URL.
