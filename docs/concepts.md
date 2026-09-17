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

## Added in Phase 4

**State machine.** A set of states plus the transitions permitted between them.
Making the permitted set explicit turns "what should happen next" from a
judgement each piece of code makes separately into one rule everything shares.

**Lifecycle.** Modelling a case as a position in a process rather than as a
stored result. Lets you ask "which cases are in a state they should have left by
now?", which is the question every automated recovery mechanism depends on.

**Terminal state.** One with no outgoing transitions. `decided` is ours: once a
decision exists, nothing a vendor says afterwards may move the application.

**Out-of-order delivery.** Messages arriving in a different order from the one
they were sent in. Normal on any network, and guaranteed once retries exist.

**Stale result.** A genuine message describing a state that has since been
superseded. Correctly signed, correctly formed, and wrong to apply.

**Recency guard.** Comparing the vendor's own timestamp for a result against the
vendor's timestamp for the last result applied, and ignoring anything older.
Vendor clock against vendor clock — comparing their event time to our
observation time mixes two clocks and fails silently under skew.

**Clock skew.** Two machines disagreeing about the time. The reason a guard
built on someone else's clock is a weaker guarantee than one built on rules you
control.

**Sweeper (reconciliation job).** A periodic job that compares what we believe
against what a third party knows, and repairs the difference. It exists because
webhooks are a delivery attempt, not a guarantee.

**Push versus pull.** Push (webhooks) is fast but unreliable. Pull (the sweeper)
is slow but complete. Systems that need both use push for latency and poll for
correctness.

**Adapter.** A single interface with one implementation per external provider,
so the code that uses it never names a vendor. What lets the same handler run
against a simulator and a real API.

**Reconciliation.** Generally, the practice of periodically checking your own
records against an external source of truth. Ubiquitous in finance — this is a
small version of what payment firms do against their bank statements nightly.

## Added in Phase 5

**SDN — Specially Designated Nationals.** The US Treasury's sanctions list.
Public domain, since it is a work of the US Government, and the list with the
most legal force behind it worldwide.

**FATF — Financial Action Task Force.** The intergovernmental body that
publishes the two country lists everyone screens against: "high-risk
jurisdictions subject to a call for action" (the severe list) and "jurisdictions
under increased monitoring" (the grey list).

**De-risking.** Refusing or dropping whole categories of customer — PEPs, or
everyone from a particular country — rather than assessing them individually.
Cheaper than doing the work, and something regulators criticise, because it
pushes people out of the banking system altogether.

**Enhanced due diligence (EDD).** The extra work the law requires for
higher-risk customers: senior sign-off, establishing source of wealth, ongoing
monitoring. What a PEP match triggers. Not refusal.

**Strict liability.** Liability without needing to prove intent. Sanctions
breaches are strict-liability offences, which is why "we assessed the risk and
proceeded" is not a defence.

**Fuzzy matching.** Comparing strings by similarity rather than equality,
returning a strength rather than yes/no.

**Transliteration.** Writing a name from one script in another. There is often
no single correct answer — محمد is legitimately Mohammed, Muhammad, Mohamed or
Mohammad — which is the root cause of most name-matching difficulty.

**Patronymic.** A name element derived from the father's name: Russian
*Vladimirovich*, Icelandic *-son* / *-dóttir*. Present or absent depending on
which document you are reading, so it must not break a match.

**AKA / alias.** Alternative spellings the list itself publishes. More valuable
than any threshold: a transliteration pair that scores 80 by string similarity
becomes an exact match if the list carries both spellings.

**token_set_ratio.** A scorer that ignores word order and extra words. Handles
"Xi Jinping" against "Jinping Xi" — and returns 100 whenever one name's words
are a subset of the other's, which is how a list entry reading "KHAN" comes to
match "Sarah Khan".

**Token alignment.** Requiring that a minimum number of name *parts* correspond
before two names are considered a candidate pair at all. The fix for the
containment problem above, and roughly how a person compares two names.

**Noise floor.** The similarity below which a pair is not recorded at all. Not a
decision threshold: everything above it is recorded, and what each band is
*worth* is decided separately.

**False positive / false negative, asymmetrically.** A false negative here is a
missed sanctions match — a regulatory breach. A false positive is a real
customer turned away and an officer's hour burned. Both are costly; only one is
a criminal offence, and that asymmetry decides every threshold in this phase.

**Recall.** The proportion of genuine matches actually caught. The number that
matters most, and the one a tidy-looking approve/review/reject split hides.

**Pure function.** One whose output depends only on its arguments, with no
side effects — no database, no network, no clock. Makes results deterministic,
reproducible and testable without fixtures.

**Points-based scoring (rules engine).** Adding up explicit, individually
justifiable signals to reach a score, as opposed to a model. Chosen here
because the output must be an argument a human and a regulator can follow,
not a prediction.

**Ruleset version.** A label stored with every score identifying which version
of the rules produced it, so an old decision can be reproduced after the rules
change.

## Added in Phase 6

**Compliance officer.** The person who decides the cases automation would not.
Their actual question is narrower than it sounds: is this the person on the
list, or someone who happens to share their name?

**Four-eyes principle.** Requiring a second person to approve certain
decisions. Not implemented here — worth knowing as the next control a real firm
would add above a threshold.

**Audit sampling.** How an auditor works: pull a sample of decisions and ask,
per case, whether the decision was reasonable on the evidence available at the
time. Not whether the outcome was right. This is what the mandatory reason
field exists to answer.

**Override.** A human decision that goes against what the score suggested.
Legitimate and expected — the reason field is what makes it defensible rather
than suspicious.

**`SELECT … FOR UPDATE` vs `FOR UPDATE SKIP LOCKED`.** The same row lock with
opposite behaviour when contended. `SKIP LOCKED` moves on, which is right for a
job queue where another worker will take the row. Plain `FOR UPDATE` waits,
which is right when the loser is a person who needs to be told what happened.

**Optimistic vs pessimistic concurrency.** Pessimistic takes the lock first
(what the desk does). Optimistic writes and lets a constraint reject the loser
(what the unique index does). Here both are used: the lock for a good error
message, the constraint for the guarantee.

**Backstop constraint.** A database rule that holds even when every layer above
it has a bug. `decisions_one_terminal_per_application` is what makes deciding
twice impossible rather than merely discouraged.

**Signed cookie.** A session value with an HMAC attached, so the server can tell
it produced it. Signed, not encrypted: it prevents forgery, it does not hide the
contents — which is fine when the contents are not secret.

**`httpOnly`.** A cookie flag making the value unreadable from JavaScript, so a
cross-site scripting bug elsewhere cannot steal the session.

**`SameSite=lax`.** A cookie flag stopping the browser sending it on cross-site
POSTs. The basic defence against cross-site request forgery.

**CSRF — cross-site request forgery.** Tricking a logged-in user's browser into
submitting a request to your site from somewhere else.

**Server Action as a public endpoint.** A Next.js Server Action is a POST
endpoint independent of any page. Guarding the page that renders it does not
guard the action; the check has to be inside the action itself.

**Session revocation.** Cancelling a session before it expires. A signed cookie
with no server-side record cannot be revoked, which is a real limitation of the
simple approach used here.
