"""SQeuaL — text-to-SQL where the model never writes a number.

A language model is good at turning "how much did we refund in Berlin last
month" into a SELECT, and bad at arithmetic it has to do in its head. So it is
allowed to do exactly the first thing, and its output is treated as what it is:
untrusted input. Deterministic code parses it, checks every table and column
against the real schema, rewrites it to carry a row limit, runs it against a
read-only connection, and renders the answer from the rows that come back.

Phase A is the deterministic half: `db`, `schema`, `guard`, `execute`. No stage
in it calls a model.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
