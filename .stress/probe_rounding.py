import sqlite3
import struct

from app import db as d

c = sqlite3.connect(":memory:")
c.execute("CREATE VIRTUAL TABLE rt USING rtree(id, minx, maxx, miny, maxy)")

samples = [-14.6, -15.0, -69.6, 11.4, 0.4, -84.79999999999998, 116.3, 39.7]
for i, v in enumerate(samples):
    c.execute("INSERT INTO rt VALUES (?, ?, ?, ?, ?)", (i + 1, v, v, v, v))

for i, v in enumerate(samples):
    row = c.execute("SELECT minx, maxx, miny, maxy FROM rt WHERE id=?", (i + 1,)).fetchone()
    bits = lambda x: struct.unpack("I", struct.pack("f", x))[0]
    print(f"v={v!r:>22} stored={row[0]!r:>22}  n32={d._as_float32(v)!r:>22} "
          f"floor32={d._floor32(v)!r:>22} ceil32={d._ceil32(v)!r:>22} "
          f"match floor={row[0]==d._floor32(v)} ceil={row[0]==d._ceil32(v)} n={row[0]==d._as_float32(v)}")
