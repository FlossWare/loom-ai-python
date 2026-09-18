# loom-ai-python

Python implementation of the AI-domain contracts defined by FlossWare/loom-ai.

The repository layering is:

    loom
      |
      +-- loom-python
      |
      +-- loom-ai
             |
             +-- loom-ai-python

loom defines the language-neutral protocol. loom-ai defines AI-domain contracts and semantics. This repository provides their Python implementation.

Future implementations such as loom-ai-java and loom-ai-erlang are peers.

See FlossWare engineering standard ADR-0024 for the contract-centric repository naming and layering convention.
