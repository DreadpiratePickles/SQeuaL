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
"""

from .card import SchemaCard

NO_TABLES = "_No tables matched. Nothing was selected._\n"


def _column_row(column) -> str:
    key = "PK" if column.primary_key else ""
    nullable = "yes" if column.nullable else "no"
    samples = ", ".join(column.sample_values)
    return f"| {column.name} | {column.type} | {nullable} | {key} | {samples} |"


def render_card(card: SchemaCard, tables: tuple[str, ...] | None = None) -> str:
    """Render `card`, or the named subset of it, as Markdown.

    Args:
        card: the card to render.
        tables: names to render, in the order they should appear. `None`
            renders every table in card order. A name the card does not know is
            skipped rather than invented — the caller's mistake must not become
            a table in a prompt.
    """
    if tables is None:
        selected = list(card.tables)
    else:
        resolved = [card.table(name) for name in tables]
        selected = [table for table in resolved if table is not None]

    visible = {table.name.lower() for table in selected}

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

    for table in selected:
        lines += [
            f"## {table.name} ({table.row_count} rows)",
            "",
            "| column | type | null | key | sample values |",
            "|---|---|---|---|---|",
        ]
        lines += [_column_row(column) for column in table.columns]
        edges = [fk for fk in table.foreign_keys if fk.to_table.lower() in visible]
        if edges:
            lines.append("")
            lines += [
                f"- {fk.from_table}.{fk.from_column} -> {fk.to_table}.{fk.to_column}"
                for fk in edges
            ]
        lines.append("")

    return "\n".join(lines)
