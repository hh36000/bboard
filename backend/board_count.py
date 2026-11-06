"""One definition for boards included in homepage and admin counts."""

# A revision-zero board with no visible text has never been used. Once a board
# has text or advances beyond revision zero, it remains part of the count.
COUNTED_BOARD_SQL = "(revision <> 0 OR content ~ '[^[:space:]]')"
