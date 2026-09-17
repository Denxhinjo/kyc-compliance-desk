# Concepts

A plain-English glossary of terms used in this project. Fintech terms and
engineering terms both — anything worth defining once.

## Fintech

**KYC — Know Your Customer.** The legally required process of establishing that
a customer is who they say they are before you let them use a financial
service. In practice: collect name, date of birth and address, verify an
identity document, and check the person against various lists.

**AML — Anti-Money Laundering.** The wider regulatory regime KYC sits inside.
Its purpose is to stop criminals moving money through regulated businesses.
KYC is one AML control; sanctions screening and transaction monitoring are
others.

**Applicant.** A person going through the onboarding process. Not yet a
customer — they become one only if the checks pass.

**Compliance officer.** The human who reviews cases the system could not decide
automatically. The review desk in Phase 6 is built for this person.

**Synthetic data.** Fabricated data that looks realistic but describes no real
person. Everything in this project is synthetic — using real identity data for
a portfolio demo would itself be a data-protection problem.

## Engineering

**Migration.** A numbered SQL file describing one change to the database
structure. Applied in order, migrations build the schema from nothing. They are
to the database what commits are to the code.

**ORM — Object-Relational Mapper.** A library that generates SQL from objects
in your programming language (Prisma, SQLAlchemy). This project does not use
one: the schema is shared by two languages, so it must not be owned by either.

**Connection pool.** A set of database connections kept open and reused. Opening
a connection costs a network round trip plus authentication, so paying that per
query would be wasteful. `/web` uses a pool because it serves many requests at
once; `/worker` holds a single long-lived connection because it does one thing
at a time.

**Server Component.** React that runs only on the server; its code is never
sent to the browser. That is what lets a page query Postgres directly without
an API route in between.

**Walking skeleton.** A build where every component exists and every connection
between them is proven, but nothing does useful work yet. Phase 0 is one.

**`pg_stat_activity`.** A Postgres system view listing every current connection.
Setting `application_name` on a connection makes it identifiable there — which
is how we showed `kyc_web` and `kyc_worker` hitting the same database.

**Health check (Docker).** A command Docker runs repeatedly to decide whether a
container is genuinely usable, not merely started. Ours runs `pg_isready`;
without it, "container up" would not mean "database ready".

## Added in Phase 1

**Sanctions list.** A government-published list of people and entities you are
forbidden to do business with — US OFAC, the EU consolidated list, UK HMT. A
name appearing on one is not a suspicion, it is a prohibition.

**PEP — Politically Exposed Person.** Someone in a prominent public role, or
close to one. Not disqualifying, and not an accusation: it triggers extra
scrutiny because such people carry higher bribery and corruption risk.

**Idempotency key.** A value that identifies a message uniquely, so that
receiving it twice has the same effect as receiving it once. Ours is
`(vendor, vendor_event_id)`, enforced by a unique constraint.

**Append-only.** Rows are inserted, never updated or deleted. A correction is a
new row recording the correction, not an edit to the original.

**WORM — write-once-read-many.** Storage that physically cannot be rewritten.
Where audit records ultimately belong, because a database superuser can always
disable a database trigger.

**Transactional DDL.** Postgres can `CREATE TABLE`, `ALTER` and `INSERT` inside
one transaction and roll them all back together. Many databases cannot. It is
why a migration here never half-applies.

**Advisory lock.** A lock on an arbitrary number rather than on a row or table,
used to make sure only one process does something at a time. The migration
runner takes one so two deploys cannot migrate simultaneously.

**SQLSTATE.** Postgres' standard five-character error code. `23505` is a unique
violation (how duplicate webhooks are detected), `23514` a check constraint,
`28P01` a bad password, `3D000` no such database.

**Savepoint.** A marker inside a transaction you can roll back to without
abandoning the whole transaction. Relevant because a nested
`conn.transaction()` in psycopg becomes a savepoint rather than a real
transaction — which is how a test can appear to write data and actually write
nothing.

**Statement-level vs row-level trigger.** A row-level trigger fires once per
affected row; a statement-level one fires once per statement, even when it
affects no rows. The append-only triggers are statement-level so that a
`DELETE` matching nothing is still refused.

**Migration checksum.** A hash of a migration file recorded when it is applied,
so that editing an already-applied migration is detected instead of silently
leaving the database and the repository disagreeing.

**Partial index.** An index covering only the rows that match a `WHERE` clause.
A partial *unique* index enforces uniqueness over just that subset — which is
how `decisions` allows many referrals but only one terminal verdict per case.

## Added in Phase 2

**Race condition.** A bug where the result depends on the exact timing of two
things happening at once. Dangerous because it is usually rare: it passes every
test and fails in production under load.

**Row-level lock (`FOR UPDATE`).** Claiming a row so no other transaction can
change it until yours commits. Other transactions that want the same row wait.

**`SKIP LOCKED`.** An addition to `FOR UPDATE` telling Postgres to ignore rows
that are already locked rather than waiting for them. It is what turns a table
into a queue that many workers can share.

**Transactional enqueue.** Inserting a job in the same database transaction as
the data it refers to, so the two can never come apart. The main reason the
queue lives in Postgres rather than in Redis.

**Exponential backoff.** Waiting longer after each successive failure — 5s,
10s, 20s — so that retrying a service that is down does not make its outage
worse.

**Jitter.** A random wobble added to a retry delay. Without it, everything that
failed during one outage retries at the same instant when it ends.

**Thundering herd.** The resulting stampede, which can knock over the service
that had just recovered.

**Dead letter queue.** Where work goes after automatic recovery has been
exhausted, so a human can look at it. Our `parked` status.

**Poison pill.** A job that reliably crashes whatever processes it. Counting
attempts at claim time rather than at failure time is what stops one from
killing every worker in turn, forever.

**At-least-once delivery.** The guarantee you actually get from any queue: a
job may run more than once. Exactly-once does not exist across a process
boundary, because external side effects cannot be rolled back.

**Idempotent.** Safe to do twice with the same result as doing it once. The
obligation that at-least-once delivery places on every handler.

**WAL — write-ahead log.** Postgres records every change here before applying
it. Relevant because each job claim is a write, so a very high-throughput queue
burns real database capacity.

**Dead tuple.** The old version of a row left behind by an `UPDATE`, cleaned up
later by vacuum. Queue tables generate many, which is one of the honest limits
of using a table as a queue.

**`LISTEN`/`NOTIFY`.** Postgres' built-in publish/subscribe, which would let
workers be woken instantly instead of polling. The upgrade path when polling
latency starts to matter.

## Added in Phase 3

**Webhook.** A message a vendor POSTs to a URL of yours when something happens,
instead of you repeatedly asking them whether anything has. Push rather than
poll.

**Webhook signature.** An HMAC of the message body, computed with a secret only
you and the vendor share. Proves the message came from them and was not altered
on the way. Prevents forgery — without it, anyone who finds your public URL can
post "this applicant is approved".

**HMAC.** Hash-based Message Authentication Code: a hash of a message combined
with a secret key, so only someone holding the key can produce or check it.

**Replay attack.** Resending a genuine, correctly signed message. A signature
cannot detect it, because the message really is genuine. Defended against by a
timestamp freshness window plus a unique constraint on the event id.

**Timing attack.** Learning a secret from how long a comparison takes. String
equality returns at the first differing byte, so it runs longer the more
leading bytes match. `crypto.timingSafeEqual` compares the whole buffer every
time.

**Constant-time comparison.** A comparison whose duration does not depend on
where the inputs differ. The defence against the above.

**Raw body.** The exact bytes of an HTTP request, before any parsing. What a
signature is computed over, and therefore what it must be verified against —
parsing and re-serialising produces different bytes.

**Idempotency key.** The value that identifies a message uniquely so that
receiving it twice has the same effect as once. Didit supplies one as
`event_id`.

**Progressive enhancement.** Building so the page works without JavaScript and
improves when JavaScript is available. A Next.js form submits to the server
action directly when scripting is off — but only if the action is passed as a
direct reference rather than wrapped in a client-side closure.

**Server Action.** A function marked `"use server"` that the browser can call
directly, with no API route written by hand. The body never reaches the
browser. Every export in such a file becomes a callable endpoint, which is why
they must all be async.

**PEP / sanctions screening.** See Phase 1 — relevant here because a vendor
returning "Approved" means the *document check* passed, not that the customer
may be onboarded. Those are different claims and the system keeps them apart.
