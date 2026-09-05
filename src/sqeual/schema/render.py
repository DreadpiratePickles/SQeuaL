"""Stage 02: render a schema card into the text a model is shown.

This is a prompt, not a report, and it is written to be read by something that
guesses. Three things earn their place in it:

  * **exact column names and types**, because a model that has to guess whether
    the column is `total` or `total_cents` will guess;
  * **sample values for enum-shaped columns**, because `status = 'REFUNDED'`
    returns zero rows and looks like a correct answer;
  * **the foreign keys**, because the join a question needs is the single thing
    a model gets wrong most often, and naming the edges is cheaper than hoping.

Rendering is deterministic — same card, same bytes — so a change in what the
model was shown is always a diff somebody can read.

When a subset is rendered, foreign keys pointing *out* of the subset are
omitted. Naming a join to a table the model was not shown is an invitation to
write SQL against a table that is not there.

`[guard] denied_columns` is rendered as a sixth column, and only when one of the
tables on show actually has a denied column — a card that says nothing costs no
tokens, and a deployment that denies nothing sees exactly the card it saw
before. It is marked here as well as enforced in the guard because the two are
different layers and both are needed: telling a model a column is off limits is
a prompt, and refusing the statement that names it anyway is a control. §12
already keeps a per-row column out of the *sample values*; a column absent from
the samples is still a column the card lists by name and type, and the first
live evaluation is where that gap was demonstrated rather than argued.
"""

from .card import SchemaCard

NO_TABLES = "_No tables matched. Nothing was selected._\n"

DENIED_MARK = "denied"
DENIED_NOTE = (
    "A column marked `denied` **may not appear** in the SELECT list, the ORDER BY "
    "or the GROUP BY, and a statement that names one there is refused before it "
    "runs. Answer with an aggregate or another column instead."
)


def _column_row(column, denied: frozenset[str], marking: bool) -> str:
    key = "PK" if column.primary_key else ""
    nullable = "yes" if column.nullable else "no"
    samples = ", ".join(column.sample_values)
    row = f"| {column.name} | {column.type} | {nullable} | {key} | {samples} |"
    if not marking:
        return row
    mark = DENIED_MARK if column.name.lower() in denied else ""
    return f"{row} {mark} |"


def _denied_on(table, denied_columns: frozenset[str]) -> frozenset[str]:
    prefix = f"{table.name.lower()}."
    return frozenset(
        entry[len(prefix):] for entry in denied_columns if entry.startswith(prefix)
    )


def render_card(
    card: SchemaCard,
    tables: tuple[str, ...] | None = None,
    *,
    denied_columns: frozenset[str] = frozenset(),
) -> str:
    """Render `card`, or the named subset of it, as Markdown.

    Args:
        card: the card to render.
        tables: names to render, in the order they should appear. `None`
            renders every table in card order. A name the card does not know is
            skipped rather than invented — the caller's mistake must not become
            a table in a prompt.
        denied_columns: `[guard] denied_columns`, lowercased `table.column`. A
            denied column on a rendered table is marked and the marking is
            explained in words; an empty set, or one naming only tables outside
            the subset, renders the card unchanged.
    """
    if tables is None:
        selected = list(card.tables)
    else:
        resolved = [card.table(name) for name in tables]
        selected = [table for table in resolved if table is not None]

    visible = {table.name.lower() for table in selected}
    denied = {table.name: _denied_on(table, denied_columns) for table in selected}
    # Only when it says something. A sixth column of empty cells in every card
    # would cost tokens in every prompt and buy nothing.
    marking = any(denied.values())

    lines = [
        f"# Database schema (`{card.schema_sha256[:12]}`)",
        "",
    ]
    if not selected:
        lines.append(NO_TABLES)
        return "\n".join(lines)

    plural = "table" if len(selected) == 1 else "tables"
    lines += [
        f"{len(selected)} {plural}. Sample values are drawn from the data and are exact: "
        "use them verbatim rather than guessing at spelling or case.",
        "",
    ]
    if marking:
        lines += [DENIED_NOTE, ""]

    for table in selected:
        lines += [
            f"## {table.name} ({table.row_count} rows)",
            "",
            "| column | type | null | key | sample values |"
            + (" policy |" if marking else ""),
            "|---|---|---|---|---|" + ("---|" if marking else ""),
        ]
        lines += [
            _column_row(column, denied[table.name], marking) for column in table.columns
        ]
        edges = [fk for fk in table.foreign_keys if fk.to_table.lower() in visible]
        if edges:
            lines.append("")
            lines += [
                f"- {fk.from_table}.{fk.from_column} -> {fk.to_table}.{fk.to_column}"
                for fk in edges
            ]
        lines.append("")

    return "\n".join(lines)
