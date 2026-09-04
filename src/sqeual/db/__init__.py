"""Stage 01: a deterministic, seeded support/e-commerce database.

The generator is committed and the database is not. A fixed seed and a fixed
sequence of draws make `sqeual db build` reproducible, which is what lets a
test assert a row count and a document quote a figure.
"""
