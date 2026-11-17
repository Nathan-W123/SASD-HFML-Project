"""Generated sewer networks for comparing old and new selection code.

Each scenario is a traced chain of mains ending at the HFML's downstream
manhole, a downstream main and a parallel distractor main, laterals on all
of them at deliberately awkward offsets (on the main, 0.3 ft, exactly 1 ft,
1.0009 ft, 1.2 ft, 3 ft), laterals tied in only at the downstream manhole, a
line crossing a main mid-span, and parcels that contain a lateral end, share
an edge at it, sit just off it, or are out of reach.
"""
import math, random

import stub_arcpy as S


def line(*pts): return S.Geom("line", [S.Pt(x, y) for x, y in pts])
def rect(x1, y1, x2, y2): return S.Geom("rect", (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))

def scenario(seed, with_laterals=True):
    rnd = random.Random(seed)
    ml, ll, parcels = {}, {}, {}
    nid = {"ml": 100, "ll": 5000, "p": 90000}
    def add(store, key, geom):
        nid[key] += rnd.randint(1, 3)          # non-contiguous OIDs, like real data
        store[nid[key]] = {"SHAPE": geom}
        return nid[key]

    # traced chain: P0 is the HFML's downstream manhole; seed runs P1 -> P0
    pts = [(0.0, 0.0)]
    heading = rnd.uniform(-0.3, 0.3)
    for _ in range(rnd.randint(2, 6)):
        heading += rnd.uniform(-0.6, 0.6)
        x, y = pts[-1]
        L = rnd.uniform(180, 380)
        pts.append((x + L * math.cos(heading), y + L * math.sin(heading)))
    traced = []
    for i in range(len(pts) - 1):
        traced.append(add(ml, "ml", line(pts[i + 1], pts[i])))
    # downstream main (not traced) and a parallel distractor main
    dn = add(ml, "ml", line(pts[0], (-300.0, rnd.uniform(-40, 40))))
    off = rnd.choice([-35.0, 35.0])
    distractor = add(ml, "ml", line((pts[0][0], pts[0][1] + off), (pts[-1][0], pts[-1][1] + off)))

    def lateral_on(main_pts, t, jitter, side, length, reverse):
        (ax, ay), (bx, by) = main_pts
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy)
        ux, uy = dx / n, dy / n
        px, py = -uy * side, ux * side
        mx, my = ax + dx * t, ay + dy * t
        s = (mx + px * jitter, my + py * jitter)
        f = (mx + px * (jitter + length), my + py * (jitter + length))
        return (f, s) if reverse else (s, f), (px, py)

    far_ends = []
    if with_laterals:
        mains = [(pts[i + 1], pts[i]) for i in range(len(pts) - 1)]
        mains.append((pts[0], (-300.0, 0.0)))                               # downstream main
        mains.append(((pts[0][0], pts[0][1] + off), (pts[-1][0], pts[-1][1] + off)))  # distractor
        for main in mains:
            for _ in range(rnd.randint(3, 9)):
                jitter = rnd.choice([0.0, 0.0, 0.0, 0.3, 0.9, 1.0, 1.0009, 1.2, 3.0])
                (s, f), perp = lateral_on(main, rnd.uniform(0.08, 0.92), jitter,
                                          rnd.choice([-1, 1]), rnd.uniform(25, 60), rnd.random() < 0.3)
                add(ll, "ll", line(s, f))
                far = f if s != f else s
                # the parcel-side end is whichever is away from the main
                far_ends.append((f if math.hypot(*(a - b for a, b in zip(f, main[0]))) > 0 else s, perp))
        # tied in only at the downstream manhole
        for _ in range(rnd.randint(0, 2)):
            ang = rnd.uniform(0, 2 * math.pi)
            add(ll, "ll", line(pts[0], (pts[0][0] + 40 * math.cos(ang), pts[0][1] + 40 * math.sin(ang))))
        # a line crossing a traced main mid-span, ends far from it
        (ax, ay), (bx, by) = mains[0]
        mx, my = (ax + bx) / 2, (ay + by) / 2
        add(ll, "ll", line((mx - 20, my - 20), (mx + 20, my + 20)))
        # a lateral exactly on a traced manhole between two traced mains
        if len(pts) > 2:
            add(ll, "ll", line(pts[1], (pts[1][0] + 15, pts[1][1] + 30)))

        # parcels around every lateral's outer end
        for row in list(ll.values()):
            g = row["SHAPE"]
            for end in (g.firstPoint, g.lastPoint):
                kind = rnd.random()
                x, y = end.X, end.Y
                if kind < 0.6:      # contains the end
                    add(parcels, "p", rect(x - rnd.uniform(5, 30), y - rnd.uniform(5, 30),
                                           x + rnd.uniform(5, 30), y + rnd.uniform(5, 30)))
                elif kind < 0.75:   # end on a shared edge between two parcels
                    add(parcels, "p", rect(x - 30, y - 20, x, y + 20))
                    add(parcels, "p", rect(x, y - 20, x + 30, y + 20))
                elif kind < 0.9:    # end just outside: nearest-parcel fallback
                    gap = rnd.uniform(0.5, 4.5)
                    add(parcels, "p", rect(x + gap, y - 15, x + gap + 30, y + 15))
                else:               # nothing within 5 ft
                    add(parcels, "p", rect(x + 8, y + 8, x + 40, y + 40))
        # filler parcels scattered around
        for _ in range(30):
            x, y = rnd.uniform(-400, 1500), rnd.uniform(-600, 600)
            add(parcels, "p", rect(x, y, x + rnd.uniform(20, 60), y + rnd.uniform(20, 60)))

    data = {"ml": ml, "ll": ll, "parcels": parcels}
    return data, traced, pts[0]
