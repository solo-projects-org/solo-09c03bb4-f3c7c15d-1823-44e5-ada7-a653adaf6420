from app import db as d
from app.config import get_settings

conn = d._connect(get_settings().db_path)
conn.executescript(d.SCHEMA)
use = d._create_rtree_if_supported(conn)
conn.execute("BEGIN IMMEDIATE")
d.rebuild_current_index(conn, use)
print("rtree:", use, "drift after rebuild:", d.current_index_drift(conn, use))
conn.rollback()
conn.close()
