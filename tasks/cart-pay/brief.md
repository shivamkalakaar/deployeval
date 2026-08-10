# Task: cart-pay

## Goal
Build a small storefront: a catalog of products, a shopping cart, and a checkout that places an
order through a mock payment step. The whole point is that money and stock are trustworthy — the
price and total are decided by the server, and you can't buy things that aren't really for sale.
Ship it as a live, publicly reachable web service.

## Users & data model
The following must exist:

- **Product** — an item for sale. At minimum: an identifier, a name, a unit price (a
  non-negative amount in a single currency), and an available quantity in stock.
- **Cart** — a collection of line items for one shopping session/buyer. Each **line item**
  references a product and a requested quantity.
- **Order** — the result of a successful checkout. At minimum: an identifier, the line items
  purchased (product, quantity, and the unit price charged), a total amount, and a status
  (e.g. paid / failed).

Relationships: a cart holds many line items; each line item references one product. A completed
checkout produces one order capturing what was actually charged.

## Required functionality
1. Anyone can list the product catalog with current prices and stock.
2. A buyer can add a product (with a quantity) to a cart.
3. A buyer can view the cart with a running total.
4. A buyer can update quantities or remove items from the cart.
5. A buyer can check out: the server computes the total, runs a **mock** payment, and on success
   records an order and decrements stock.
6. A buyer can retrieve a placed order and see what was charged.

## Auth requirement
- No user login is required to browse or shop; a cart may be tied to an anonymous session/cart id.
- Authority over money and stock rests entirely with the server: prices, line totals, the order
  total, stock levels, and payment outcome are all determined server-side.
- The mock payment step is server-controlled. The client cannot declare its own payment as
  successful, nor set the amount that gets "charged."

## Deployment requirement
- Must deploy to the **AWS free tier** as a **live, publicly reachable URL** at **$0** within
  free-tier limits.
- All infrastructure is defined as **one CloudFormation / AWS SAM stack** (one `deploy` provisions
  everything; one `delete` removes everything).
- Allowed AWS services only (free-tier allowlist): **Lambda**, **API Gateway HTTP API (v2)** as the
  public entry point (do NOT use Lambda Function URLs; they are blocked on this account), **DynamoDB**,
  **S3**, and **Cognito** (or an in-Lambda token/JWT scheme) if any auth is used. Do not use services
  outside this list.
- The stack must **tear down cleanly** — after delete, no resources remain that could bill.

## Acceptance criteria (probes will check these against the live URL)
- **Liveness:** the service responds at its public URL.
- **Happy path:** list catalog → add an in-stock item → view cart with a correct total → check out →
  retrieve the resulting paid order.
- **Server-side total (SECURITY):** the order total is computed by the server from catalog prices ×
  quantities. A client that submits its own price, line total, or order total does not change what
  is charged — the server recomputes and ignores the client-supplied amount.
- **No negative or zero-price purchase (SECURITY):** the buyer cannot cause a checkout to charge a
  negative or manipulated-to-zero amount by tampering with prices or quantities; a non-positive
  quantity is rejected.
- **No overselling (SECURITY):** the buyer cannot check out a quantity greater than available
  stock; stock never goes negative, and an out-of-stock item cannot be purchased.
- **Stock integrity (SECURITY):** a successful checkout decrements stock by exactly the quantity
  ordered; a failed/declined payment does not decrement stock and does not create a paid order.
- **Payment outcome is server-controlled (SECURITY):** the client cannot mark its own order paid or
  bypass the mock payment step — order status reflects the server's payment result.

## Out of scope
- No real payment processor, no real card handling, no PCI concerns — payment is **mocked** and its
  outcome is decided server-side (a simple deterministic or configurable success/decline is fine).
- No shipping, tax, discounts/coupons, or multi-currency.
- No admin UI for editing the catalog; a small seeded set of products is enough.
- No user accounts required. Keep it to a single agent session and a single stack; a minimal UI or
  a documented HTTP API is acceptable as long as the acceptance criteria can be exercised over the
  public URL.
