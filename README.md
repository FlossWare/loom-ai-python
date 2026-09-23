# loom-ai-python

**Python implementation of the AI-domain contracts defined by FlossWare/loom-ai.**

Repository layering:

    loom
      |
      +-- loom-python
      |
      +-- loom-ai
             |
             +-- loom-ai-python

- loom defines the language-neutral Loom protocol and semantic contract.
- loom-python implements the foundational Loom contract in Python.
- loom-ai defines AI-domain contracts and semantics.
- loom-ai-python implements those AI-domain contracts in Python.

Python is one implementation language among peers. Future implementations such as
loom-ai-java and loom-ai-erlang implement the same loom-ai contracts without
depending on Python internals.

## Ownership

This repository owns:

- Python implementations of AI-domain contracts.
- Python runtime bindings and transport implementations.
- Python-specific tests and packaging.
- Python-specific dogfood and development tooling.

FlossWare/loom-ai remains authoritative for AI-domain contract semantics.
Generic Loom protocol semantics remain authoritative in FlossWare/loom.

## Current implementation

The Python implementation currently provides:

- Intent
- Worker and WorkerContext/WorkerResult
- Arbiter composition
- provider-neutral ModelProvider boundary
- ModelWorker
- deterministic FakeModelProvider
- HTTP transport binding
- conformance-oriented tests and dogfood

These are Python realizations. They do not define the underlying AI-domain contracts.

## Architectural boundary

AI-specific behavior may extend the Loom execution model, but it must not redefine
or contradict generic Loom semantics.

Provider SDKs, credentials, model routing, budgets, caching, evaluation,
optimization strategies, and other reusable capabilities remain separate
capabilities rather than becoming implicit responsibilities of this repository.

## Durable dogfood server profile

The default `loom-server` entrypoint remains a transport-smoke server. It intentionally
uses a NoOp Worker and does not claim to provide repository execution or durable
process recovery.

For process-boundary qualification, use the committed dogfood profile:

    python scripts/dogfood-server.py \\
      --root /path/to/task-repository \\
      --target /path/to/task-repository/tests/test_server.py \\
      --state-dir /path/to/durable-state \\
      --python /path/to/loom-ai-python/.venv/bin/python

The profile composes the public HTTP boundary, `FileExecutionStateStore`, Arbiter,
a real repository-changing Worker, and a verification Worker. It is deliberately
a qualification profile, not a production workflow or a new orchestration layer.

The reproducible qualification against a fresh `FlossWare/loom-ai` checkout is:

    bash scripts/dogfood-process-boundary.sh

The qualification demonstrates submit, durable persistence, verification, Loom
process termination, restart, observation by stable `execution_id`, continuation,
and final verification. The consumer communicates only through the HTTP boundary.

## Development

    python -m ruff format --check .
    python -m ruff check .
    python -m pytest -q
    python -m build --wheel --sdist

See FlossWare engineering standard ADR-0024 for the contract-centric repository
naming and layering convention.


## Python import identity

The distribution is named `flossware-loom-ai-python` to make the implementation boundary explicit.

The Python import package remains `loom_ai` for compatibility with the existing Python implementation API. This import name is a language-specific implementation detail and is **not** the architectural identity or authority of the AI-domain contract.

A future `loom-ai-java` or `loom-ai-erlang` implementation does not share or depend on the Python import namespace.
