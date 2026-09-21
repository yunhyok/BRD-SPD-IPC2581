"""Associate explicit negative SPD shapes with positive contours, without tessellation."""
import json

from shapely.geometry import Point, Polygon
from shapely import prepare
from shapely.strtree import STRtree

from .geometry import el, ring, circle_ring, feature_set, num


def write_planes(db, section, output, report, scale=1):
    """One layer at a time; holes spool to SQLite instead of accumulating in RAM."""
    db.execute("CREATE TEMP TABLE IF NOT EXISTS holes(parent INTEGER, child INTEGER)")
    db.execute("DELETE FROM holes")
    positives, geometry, nets, valid = [], [], [], []
    for rowid, net, kind, encoded in db.execute(
            "SELECT rowid,net,kind,data FROM shapes WHERE section=? AND polarity='+'", (section,)):
        data = json.loads(encoded)
        if kind == "Polygon":
            shape = Polygon(list(zip(data[::2], data[1::2])))
        else:
            # ponytail: use a bounding disk only for containment lookup; emitted circles remain exact arcs.
            shape = Point(data[:2]).buffer(data[2], quad_segs=32)
        is_valid = shape.is_valid
        if not is_valid:
            report.warn("INVALID_PLANE_TOPOLOGY", f"Shape row {rowid}; emitted unchanged, void assignment needs review")
        else:
            prepare(shape)
        positives.append(rowid)
        geometry.append(shape)
        nets.append(net)
        valid.append(is_valid)
    tree = STRtree(geometry) if geometry else None
    unmatched = []
    for rowid, net, kind, encoded, line in db.execute(
            "SELECT rowid,net,kind,data,line FROM shapes WHERE section=? AND polarity='-'", (section,)):
        data = json.loads(encoded)
        hole = Polygon(list(zip(data[::2], data[1::2]))) if kind == "Polygon" else Point(data[:2]).buffer(data[2], quad_segs=32)
        hole_valid = hole.is_valid
        parents = []
        if tree is not None:
            for index in tree.query(hole):
                if nets[index] != net:
                    continue
                outer = geometry[index]
                if not valid[index] or not hole_valid:
                    continue
                if kind == "Circle":
                    center = Point(data[:2])
                    # A covered bounding square proves exact disk containment.
                    # Only boundary-adjacent circles need the more expensive exact distance.
                    covered = outer.covers(hole.envelope) or (
                        outer.covers(center) and outer.boundary.distance(center) >= data[2] - 1e-12)
                else:
                    covered = outer.covers(hole)
                if covered:
                    parents.append(positives[index])
        if parents:
            preceding = [parent for parent in parents if parent < rowid]
            if preceding:
                # SPD emits a positive physical parent followed by its negative
                # children. Confirm containment, then preserve that ownership.
                parents = [max(preceding)]
            elif len(parents) == 1:
                report.warn("VOID_ORDER_FALLBACK", f"{section}, shape row {rowid}: unique containing parent occurs later", line)
            else:
                parents = []
        if parents:
            db.executemany("INSERT INTO holes VALUES (?,?)", [(p, rowid) for p in parents])
            report.counts["plane_voids_as_cutouts"] += 1
        else:
            unmatched.append(rowid)
            report.warn("SIGNED_VOID_FALLBACK", f"{section}, shape row {rowid}: no containing positive contour on net {net}; retained as NEGATIVE Set", line)
    db.execute("CREATE INDEX IF NOT EXISTS holes_parent ON holes(parent)")
    for rowid, net, kind, encoded in db.execute(
            "SELECT rowid,net,kind,data FROM shapes WHERE section=? AND polarity='+'", (section,)):
        data = json.loads(encoded)
        contour = el("Contour")
        contour.append(ring(data, scale) if kind == "Polygon" else circle_ring(data, scale, "Polygon"))
        for child_kind, child_data in db.execute(
                "SELECT s.kind,s.data FROM holes h JOIN shapes s ON s.rowid=h.child WHERE h.parent=?", (rowid,)):
            coords = json.loads(child_data)
            contour.append(ring(coords, scale, "Cutout") if child_kind == "Polygon" else circle_ring(coords, scale))
        output(feature_set(net, contour))
        report.counts["plane_positive_features"] += 1
    for rowid in unmatched:
        net, kind, encoded = db.execute("SELECT net,kind,data FROM shapes WHERE rowid=?", (rowid,)).fetchone()
        data = json.loads(encoded)
        if kind == "Circle":
            feature = el("Circle", diameter=num(2 * data[2] * scale))
            output(feature_set(net, feature, "NEGATIVE", data[0], data[1], scale))
        else:
            contour = el("Contour")
            contour.append(ring(data, scale))
            output(feature_set(net, contour, "NEGATIVE"))
        report.counts["plane_negative_sets"] += 1
