"""Stage 02: pick the tables a question needs.

Deterministic term overlap plus one hop of foreign-key expansion. No
embeddings, no model, no similarity score.

That is a deliberate choice and not a shortcut. The slicer will sometimes pick
the wrong table, and when it does somebody has to be able to answer "why did it
pick that". With this design the answer is a rule they can read and a synonym
they can edit; with an embedding it is a cosine distance nobody can argue with
and nobody can fix without retraining something. The slice is also the input to
the guard's `allowed_tables` in Phase B, which makes it a security boundary as
well as a prompt-size optimisation — and a security boundary computed by a
nearest-neighbour search is a security boundary nobody can review.

Three tiers, in order, capped by `max_tables`:

1. **Seeds** — tables the question names, by table name, by a configured
   synonym, or by one of their column names. Ranked by evidence.
2. **Bridges** — a table adjacent by foreign key to *two* seeds. It is the join
   path, and without it the two seeds cannot be related at all. Bridges prefer
   NOT NULL edges: a join through a nullable key silently drops rows.
3. **Neighbours** — a table one foreign key from any seed. This is the tier
   that trades precision for recall on purpose: one table too few makes a
   question unanswerable, one too many costs a few hundred tokens.

A question that matches nothing yields an empty slice. It does not guess.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .card import SchemaCard, SchemaError

SCORE_TABLE_NAME = 3
SCORE_SYNONYM = 3
SCORE_COLUMN_NAME = 1
"""Naming a table is stronger evidence than sharing a column name with it.
`channel` is a column on both `orders` and `tickets`; the word "orders" in the
question should still put `orders` first."""


class SliceError(SchemaError):
    """The question or the slicing configuration cannot be used."""


@dataclass(frozen=True)
class SliceEntry:
    """One chosen table and why it was chosen."""

    table: str
    reason: str


@dataclass(frozen=True)
class SchemaSlice:
    """The chosen subset, in the order it should be shown to a model."""

    question: str
    entries: tuple[SliceEntry, ...]

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(entry.table for entry in self.entries)

    def reason(self, table: str) -> str:
        """Why `table` is in this slice.

        Raises:
            KeyError: if it is not. A reason for a table nobody chose would be
                a fabricated explanation, which is worse than no explanation.
        """
        for entry in self.entries:
            if entry.table == table:
                return entry.reason
        raise KeyError(f"{table!r} is not in this slice")


def _normalise(word: str) -> str:
    """Fold a word for matching: lowercase, and one trailing "s" removed.

    Crude on purpose. It makes "orders"/"order" and "customers"/"customer" the
    same term, which covers essentially every table name in a business schema,
    and it does so with a rule anybody can hold in their head. A real stemmer
    would also fold "address" to "addres", and the day that matters is the day
    to reach for one.
    """
    folded = word.strip().lower()
    return folded[:-1] if len(folded) > 3 and folded.endswith("s") else folded


MAX_COLUMN_WORDS = 4
"""How many consecutive question words may be joined when looking for a column
name. `unit_price_cents` is three; four is one more than anything in this
schema and keeps the search linear in the length of the question."""


def words(question: str) -> tuple[str, ...]:
    """The question's alphanumeric words, lowercased, in order, with repeats.

    Order and repeats both matter downstream: order because a tie between two
    equally-well-matched tables is broken by which the question mentioned
    first, and repeats because dropping them would shift every later position.
    """
    current: list[str] = []
    found: list[str] = []
    for character in question:
        if character.isalnum():
            current.append(character.lower())
        elif current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return tuple(found)


def tokenise(question: str) -> tuple[str, ...]:
    """The question's normalised terms, in order, without duplicates."""
    seen: set[str] = set()
    terms: list[str] = []
    for word in words(question):
        term = _normalise(word)
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return tuple(terms)


def _column_key(name: str) -> str:
    """Fold a column name for matching: lowercase, underscores removed.

    `unit_price_cents` becomes `unitpricecents`, which is what the words "unit
    price cents" join to. Somebody asking about the unit price should reach
    `products` whether or not they know the column is underscored.
    """
    return name.lower().replace("_", "")


def _validate(question: str, max_tables: int) -> None:
    if not isinstance(question, str) or not question.strip():
        raise SliceError("question must contain at least one non-whitespace character")
    if isinstance(max_tables, bool) or not isinstance(max_tables, int) or max_tables < 1:
        raise SliceError(f"max_tables must be a positive integer, got {max_tables!r}")


def _check_synonyms(synonyms: Mapping[str, str], card: SchemaCard) -> None:
    """Every synonym must name a table that exists.

    Refused rather than ignored: a synonym pointing at `invoices` in a database
    with no `invoices` is a configuration bug, and silently dropping it would
    leave the operator convinced that "invoice" reaches a table when it reaches
    nothing.
    """
    unknown = sorted({table for table in synonyms.values() if card.table(table) is None})
    if unknown:
        raise SliceError(
            f"[schema.synonyms] names table(s) this database does not have: {', '.join(unknown)}"
        )


@dataclass(frozen=True)
class _Hit:
    """One piece of evidence that a question mentions a table."""

    table: str
    position: int
    score: int
    reason: str


def _hits(question: str, card: SchemaCard, synonyms: Mapping[str, str]) -> list[_Hit]:
    """Every piece of evidence, with the word position that produced it."""
    question_words = words(question)
    found: list[_Hit] = []

    for position, word in enumerate(question_words):
        term = _normalise(word)
        if not term:
            continue
        for table in card.tables:
            if _normalise(table.name) == term:
                found.append(
                    _Hit(table.name, position, SCORE_TABLE_NAME, f"the question names {table.name}")
                )
        target = synonyms.get(term)
        if target is not None:
            resolved = card.table(target)
            if resolved is not None:
                found.append(
                    _Hit(
                        resolved.name,
                        position,
                        SCORE_SYNONYM,
                        f"the question says {term!r}, a synonym for {resolved.name}",
                    )
                )

    # Column names may be several words long, so consecutive words are joined
    # and compared against the column name with its underscores removed.
    for start in range(len(question_words)):
        for length in range(1, MAX_COLUMN_WORDS + 1):
            chunk = question_words[start : start + length]
            if len(chunk) < length:
                break
            candidates = {"".join(chunk)}
            if length == 1:
                candidates.add(_normalise(chunk[0]))
            for table in card.tables:
                for column in table.columns:
                    if _column_key(column.name) in candidates:
                        found.append(
                            _Hit(
                                table.name,
                                start,
                                SCORE_COLUMN_NAME,
                                f"the question names {table.name}.{column.name}",
                            )
                        )
    return found


def _seeds(
    question: str, card: SchemaCard, synonyms: Mapping[str, str]
) -> dict[str, tuple[int, int, str]]:
    """Table name -> (score, first mention, reason) for every table named.

    One word is one piece of evidence, however many ways it matched. "refund"
    hits both the table name `refunds` and the synonym `refund -> refunds`, and
    counting it twice would make a table look twice as relevant as an identical
    match that happens not to have a synonym entry — so hits are collapsed to
    the best score per (table, word) before they are summed.
    """
    best_per_word: dict[tuple[str, int], _Hit] = {}
    for hit in _hits(question, card, synonyms):
        key = (hit.table, hit.position)
        previous = best_per_word.get(key)
        if previous is None or hit.score > previous.score:
            best_per_word[key] = hit

    grouped: dict[str, list[_Hit]] = {}
    for hit in best_per_word.values():
        grouped.setdefault(hit.table, []).append(hit)

    result: dict[str, tuple[int, int, str]] = {}
    for table, hits in grouped.items():
        total = sum(hit.score for hit in hits)
        first = min(hit.position for hit in hits)
        # The strongest single piece of evidence is the one worth showing;
        # concatenating four reasons makes the table unreadable.
        strongest = max(hits, key=lambda hit: (hit.score, -hit.position))
        result[table] = (total, first, strongest.reason)
    return result


def _adjacency(card: SchemaCard) -> dict[str, dict[str, bool]]:
    """Undirected foreign-key adjacency: table -> neighbour -> edge is NOT NULL.

    Undirected because a join reads the same in both directions. The boolean is
    the useful part: `True` means at least one edge between the two cannot drop
    rows.
    """
    graph: dict[str, dict[str, bool]] = {table.name: {} for table in card.tables}
    for table in card.tables:
        for fk in table.foreign_keys:
            target = card.table(fk.to_table)
            if target is None:
                continue
            not_null = not fk.nullable
            for left, right in ((table.name, target.name), (target.name, table.name)):
                graph[left][right] = graph[left].get(right, False) or not_null
    return graph


def slice_for_question(
    question: str,
    card: SchemaCard,
    *,
    max_tables: int,
    synonyms: Mapping[str, str],
) -> SchemaSlice:
    """Choose the tables `question` needs, with a reason for each.

    Raises:
        SliceError: for an empty question, a non-positive `max_tables`, or a
            synonym naming a table the card does not have.
    """
    _validate(question, max_tables)
    _check_synonyms(synonyms, card)

    seeds = _seeds(question, card, synonyms)
    if not seeds:
        return SchemaSlice(question=question, entries=())

    # Best evidence first; then whichever the question mentioned earliest,
    # because "how much did we refund to customers" is about refunds and
    # mentions them first; then the name, so the order never depends on
    # dictionary iteration.
    ordered_seeds = sorted(seeds.items(), key=lambda item: (-item[1][0], item[1][1], item[0]))
    chosen: list[SliceEntry] = [
        SliceEntry(table=name, reason=reason) for name, (_score, _first, reason) in ordered_seeds
    ][:max_tables]
    seed_names = {entry.table for entry in chosen}

    graph = _adjacency(card)

    # Tier 2: bridges. A candidate adjacent to two or more chosen seeds is the
    # join path between them; without it they cannot be related at all.
    if len(chosen) < max_tables:
        bridges = []
        for candidate, neighbours in graph.items():
            if candidate in seed_names:
                continue
            linked = sorted(seed_names & set(neighbours))
            if len(linked) < 2:
                continue
            not_null_edges = sum(1 for seed in linked if neighbours[seed])
            bridges.append((len(linked), not_null_edges, candidate, linked))
        # Most seeds joined first; then the path with the most NOT NULL edges,
        # because a join through a nullable key silently drops rows; then the
        # name, so the result never depends on dictionary ordering.
        bridges.sort(key=lambda item: (-item[0], -item[1], item[2]))
        for _count, _not_null, candidate, linked in bridges:
            if len(chosen) >= max_tables:
                break
            chosen.append(
                SliceEntry(table=candidate, reason=f"joins {' to '.join(linked[:2])}")
            )

    picked = {entry.table for entry in chosen}

    # Tier 3: one foreign key from a seed. Recall, bought with tokens.
    if len(chosen) < max_tables:
        neighbours = sorted(
            {
                neighbour
                for seed in seed_names
                for neighbour in graph.get(seed, {})
                if neighbour not in picked
            }
        )
        for candidate in neighbours:
            if len(chosen) >= max_tables:
                break
            linked = sorted(seed_names & set(graph[candidate]))
            chosen.append(
                SliceEntry(
                    table=candidate,
                    reason=f"one foreign key from {linked[0]}",
                )
            )

    return SchemaSlice(question=question, entries=tuple(chosen))
