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
