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

## Development

    python -m ruff format --check .
    python -m ruff check .
    python -m pytest -q
    python -m build --wheel --sdist

See FlossWare engineering standard ADR-0024 for the contract-centric repository
naming and layering convention.
