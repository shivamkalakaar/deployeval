# Task: realtime-room

## Goal
Build a small shared real-time room: people connect to the service and land in a named room, where
they see a live list of who else is currently connected and can broadcast short text messages that
everyone else in the same room sees right away. The whole point is that delivery is live and
correctly scoped — you get messages for the room you are in and no others, and the service knows who
you are from your connection, not from what you claim. Ship it as a live, publicly reachable web
service.

## Users & data model
The following must exist:

- **User / identity** — the authenticated party behind a connection. At minimum: a stable
  identifier (and a login handle if the app has accounts). A connection's identity is established at
  connect time and is what the server attributes messages and presence to. A client does not get to
  choose its own identity on a per-message basis.
- **Room** — a named shared space, identified by an id. Many users may be in one room at once;
  messages and presence are scoped to a room.
- **Connection / presence** — a live client connection, bound to exactly one authenticated user and
  to the room(s) it has joined. The set of live connections in a room is that room's presence. A
  connection that closes is no longer present.
- **Message** — a piece of text broadcast into a room by one connected user and delivered to the
  other users currently in that room.

Relationships: a user holds a live connection; a connection joins one or more rooms; a room has many
present users; a message belongs to exactly one room and is attributed to exactly one server-derived
sender. A message with no room, or with no authenticated sender, is not valid.

## Required functionality
1. A client can open a live connection to the service.
2. A connected client can join a named room.
3. While in a room, a client can see who else is currently present (a live presence list).
4. A client in a room can broadcast a text message to that room; every other client currently in the
   same room receives it in near-real-time.
5. When a client disconnects or leaves, it is removed from the room's presence and the remaining
   clients see the updated presence.

## Auth requirement
- Opening a connection requires a valid authenticated identity, established at connect time. A
  connection that presents no credential, or a forged, tampered, or expired one, is rejected at
  connect — it is not upgraded and then silently tolerated.
- The identity of a connection is decided by the server from the credential. A client cannot claim
  to be some other user: the sender attributed to every message and every presence entry is derived
  on the server, not taken from a client-supplied field.
- A client receives messages only for rooms it has joined. Traffic for a room a client is not in must
  never reach that client — there is no cross-room delivery.
- Presence reflects only live connections; a disconnected client leaves no ghost entry.

## Deployment requirement
- Must deploy to the **AWS free tier** as a **live, publicly reachable `wss://` URL** at **$0**
  within free-tier limits.
- All infrastructure is defined as **one CloudFormation / AWS SAM stack** (one `deploy` provisions
  everything; one `delete` removes everything).
- Allowed AWS services only (free-tier allowlist): **API Gateway WebSocket API**, **Lambda**,
  **DynamoDB** (including a connection/presence table), and **Cognito** (or an in-Lambda token/JWT
  scheme) for connection auth. Do not use services outside this list. (This task is the one exception
  to the other tasks' no-API-Gateway rule: the WebSocket API is required for the realtime transport;
  no HTTP/REST API Gateway is permitted.)
- The stack must **tear down cleanly** — after delete, no resources remain that could bill
  (including the connection/presence records and the WebSocket API).

## Acceptance criteria (probes will check these against the live URL)
- **Liveness:** the service answers at its public `wss://` URL — a WebSocket connection can be
  initiated (accepted with a valid credential, or cleanly rejected without one; the endpoint responds).
- **Happy path:** two clients connect and join the same room; when one broadcasts a message, the
  other receives it in near-real-time, and each sees the other in the room's presence list.
- **Room isolation — no cross-room leakage (SECURITY):** a client receives messages only for rooms it
  joined. A message broadcast to room X is delivered to the other clients in room X and is never
  delivered to a client that only joined a different room.
- **Connection auth required (SECURITY):** a connection with no credential, or a forged, tampered, or
  expired one, is rejected at connect time — not accepted and then quietly ignored. An unauthenticated
  client cannot join a room or receive any broadcast.
- **Server-derived identity — no impersonation (SECURITY):** the sender attributed to a broadcast is
  derived by the server from the connection's authenticated identity. A client that puts another
  user's identity in the message is not believed — it cannot post as another user.
- **Presence cleanup on disconnect (SECURITY):** when a client disconnects, its presence is removed
  promptly and no ghost connection remains; the room's presence reflects only currently-live clients.

## Out of scope
- No message history or persistence — live presence and live broadcast are enough; nothing needs to
  be stored and replayed to a client that joins later.
- No direct/private messages, threads, reactions, typing indicators, read receipts, edits, or
  media/attachments in messages — plain text broadcast to a room is enough.
- No email, notifications, or any outbound/external service, and no third-party realtime provider —
  the realtime transport is the deployed stack itself.
- No moderation, roles/admin, room-management UI, or membership controls beyond join/leave; a room
  exists as soon as someone joins it by id.
- Keep it to a single agent session and a single stack; a minimal client (a small web page or a
  documented WebSocket message contract) is acceptable as long as the acceptance criteria can be
  exercised over the public URL.
