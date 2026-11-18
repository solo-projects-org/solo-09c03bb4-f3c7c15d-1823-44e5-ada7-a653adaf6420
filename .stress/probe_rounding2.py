import sqlite3

from app import db as d

c = sqlite3.connect(":memory:")
c.execute("CREATE VIRTUAL TABLE rt USING rtree(id, minx, maxx, miny, maxy)")

# (west, east, south, north) realistic boxes
boxes = [
    (-70.0, -69.6, -15.0, -14.6),
    (0.0, 0.4, 0.0, 0.4),
    (-84.79999999999998, -84.39999999999998, 39.0, 39.4),
    (116.0, 116.4, 39.3, 39.7),
]
for i, b in enumerate(boxes):
    c.execute("INSERT INTO rt VALUES (?, ?, ?, ?, ?)", (i + 1, *b))

for i, b in enumerate(boxes):
    w, e, s, n = b
    stored = c.execute("SELECT minx, maxx, miny, maxy FROM rt WHERE id=?", (i + 1,)).fetchone()
    expected = (d._floor32(w), d._ceil32(e), d._floor32(s), d._ceil32(n))
    print("in     ", b)
    print("stored ", stored)
    print("expect ", expected, "match:", stored == expected)
    print()
