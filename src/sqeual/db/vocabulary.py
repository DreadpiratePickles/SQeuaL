"""The word lists the generator draws fictional rows from.

Everything here is invented. There is no scraped dataset, no anonymised export
and no "realistic" personal data: names are assembled from two short lists of
common given names and surnames, and emails are built from those plus an
`example.invalid` domain, which RFC 2606 reserves precisely so that nothing can
route to a real mailbox.

That is a deliberate trade. A generator seeded from real support tickets would
produce a more convincing demo and would put customer text in a repository
whose whole purpose is to be read by other people. The realism this database
actually needs is *structural* — enum-shaped status columns, a nullable foreign
key, dates that respect causality, money in integer cents — and none of that
requires a real person's name.

The lists are also short on purpose. Low-cardinality columns are what make a
schema card useful: a model that can see `status` holds exactly `'refunded'`
does not have to guess `'REFUNDED'`. `[schema] max_distinct_values` is the
threshold that decides which columns get sampled, and these lists are sized to
sit comfortably under it.
"""

GIVEN_NAMES: tuple[str, ...] = (
    "Ada", "Bruno", "Clara", "Dmitri", "Elena", "Farid", "Greta", "Hugo",
    "Ingrid", "Jonas", "Katya", "Lars", "Maja", "Nils", "Olga", "Pavel",
    "Quinn", "Rosa", "Stefan", "Tomas", "Ulla", "Viktor", "Wanda", "Xenia",
    "Yusuf", "Zofia", "Anouk", "Bastien", "Cosima", "Dario",
)

SURNAMES: tuple[str, ...] = (
    "Alberti", "Bergstrom", "Castellano", "Dubois", "Engel", "Ferreira",
    "Grimaldi", "Halvorsen", "Ivanova", "Janssen", "Kowalski", "Lindqvist",
    "Moreau", "Novak", "Oberst", "Petrov", "Quesada", "Rossi", "Schneider",
    "Toivonen", "Ustinov", "Vandermeer", "Weber", "Ximenes", "Yilmaz",
    "Zieliński",
)

CITIES: tuple[tuple[str, str], ...] = (
    ("Berlin", "Germany"),
    ("Hamburg", "Germany"),
    ("Munich", "Germany"),
    ("Vienna", "Austria"),
    ("Zurich", "Switzerland"),
    ("Amsterdam", "Netherlands"),
    ("Rotterdam", "Netherlands"),
    ("Paris", "France"),
    ("Lyon", "France"),
    ("Milan", "Italy"),
    ("Rome", "Italy"),
    ("Madrid", "Spain"),
    ("Barcelona", "Spain"),
    ("Lisbon", "Portugal"),
    ("Copenhagen", "Denmark"),
    ("Stockholm", "Sweden"),
    ("Oslo", "Norway"),
    ("Helsinki", "Finland"),
    ("Warsaw", "Poland"),
    ("Prague", "Czechia"),
)
"""City paired with its country, so the two columns can never contradict each
other. Drawing them independently would put Berlin in Portugal roughly once in
twenty rows, and every geographic answer the tool produced would be defensible
arithmetic over nonsense."""

CUSTOMER_SEGMENTS: tuple[str, ...] = ("consumer", "business", "enterprise")

PRODUCT_CATEGORIES: tuple[str, ...] = (
    "audio",
    "cables",
    "keyboards",
    "lighting",
    "monitors",
    "storage",
)

PRODUCT_ADJECTIVES: tuple[str, ...] = (
    "Compact", "Portable", "Studio", "Travel", "Pro", "Nano", "Ultra", "Field",
)

PRODUCT_NOUNS: tuple[tuple[str, str], ...] = (
    ("Headphones", "audio"),
    ("Speaker", "audio"),
    ("Microphone", "audio"),
    ("HDMI Cable", "cables"),
    ("USB-C Cable", "cables"),
    ("Adapter", "cables"),
    ("Mechanical Keyboard", "keyboards"),
    ("Numpad", "keyboards"),
    ("Desk Lamp", "lighting"),
    ("Light Panel", "lighting"),
    ("Monitor", "monitors"),
    ("Monitor Arm", "monitors"),
    ("SSD Enclosure", "storage"),
    ("Memory Card", "storage"),
)

ORDER_STATUSES: tuple[str, ...] = (
    "placed",
    "shipped",
    "delivered",
    "cancelled",
    "refunded",
)

ORDER_CHANNELS: tuple[str, ...] = ("web", "mobile_app", "phone", "partner")

TICKET_CHANNELS: tuple[str, ...] = ("email", "chat", "phone", "social")

TICKET_CATEGORIES: tuple[str, ...] = (
    "billing",
    "delivery",
    "damaged_item",
    "wrong_item",
    "returns",
    "account_access",
    "product_question",
    "cancellation",
)

TICKET_PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

TICKET_STATUSES: tuple[str, ...] = ("open", "pending", "closed")

TICKET_SUBJECTS: tuple[tuple[str, str], ...] = (
    ("Charged twice for one order", "billing"),
    ("Invoice does not match the order", "billing"),
    ("Parcel has not arrived", "delivery"),
    ("Delivery is later than promised", "delivery"),
    ("Item arrived damaged", "damaged_item"),
    ("Screen has a crack in the corner", "damaged_item"),
    ("Received the wrong colour", "wrong_item"),
    ("Box contained a different product", "wrong_item"),
    ("How do I return this", "returns"),
    ("Return label never arrived", "returns"),
    ("Cannot sign in to my account", "account_access"),
    ("Password reset email never came", "account_access"),
    ("Does this work with my monitor", "product_question"),
    ("Which cable do I need", "product_question"),
    ("Please cancel my order", "cancellation"),
    ("Changed my mind before shipping", "cancellation"),
)
"""Subject paired with the category it belongs to, for the same reason cities
are paired with countries: a ticket titled "Parcel has not arrived" filed under
`billing` would make every category breakdown wrong in a way that looks fine."""

AGENT_TEAMS: tuple[str, ...] = ("frontline", "escalations", "billing", "returns")

REFUND_REASONS: tuple[str, ...] = (
    "damaged_on_arrival",
    "never_delivered",
    "wrong_item_sent",
    "changed_mind",
    "duplicate_charge",
    "late_delivery",
)

EMAIL_DOMAIN = "example.invalid"
"""RFC 2606 reserves `.invalid` so that nothing here can ever address a real
mailbox, even if somebody exports a column into a mail merge by accident."""
