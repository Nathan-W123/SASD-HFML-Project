"""Replacing a layout text element's text keeps it aligned where the template had it."""
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import types
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))   # only ArcGIS Pro has it
from backend.phase5.run import _replace_text, _set_text_elements

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

# Where an element's anchor sits across its width, per ArcGIS Pro's anchor names.
ANCHOR_AT = {"BottomLeftCorner": 0.0, "LeftMidPoint": 0.0, "TopLeftCorner": 0.0,
             "BottomMidPoint": 0.5, "CenterPoint": 0.5, "TopMidPoint": 0.5,
             "BottomRightCorner": 1.0, "RightMidPoint": 1.0, "TopRightCorner": 1.0}

class Text:
    """A point text element as ArcGIS Pro's arcpy.mp treats it: setting .text
    resizes it around its ANCHOR (elementPositionX stays put)."""
    def __init__(self, name, text, x, anchor="BottomLeftCorner", align="Center", cim="object"):
        self.name, self.anchor, self.align, self.cim = name, anchor, align, cim
        self.elementPositionX = x
        self._text = text
        self.elementWidth = self._width(text)
    @staticmethod
    def _width(text):
        return 0.1 * max(len(line) for line in text.split("\n"))
    @property
    def text(self): return self._text
    @text.setter
    def text(self, value):
        self._text = value
        self.elementWidth = self._width(value)
    def getDefinition(self, version):
        if self.cim is None:
            raise RuntimeError("no CIM")
        symbol = {"horizontalAlignment": self.align}
        if self.cim == "dict":
            return {"anchor": self.anchor, "graphic": {"symbol": {"symbol": symbol}}}
        ns = types.SimpleNamespace
        return ns(anchor=self.anchor, graphic=ns(symbol=ns(symbol=ns(**symbol))))
    def edge(self, where):
        """Page x of the element's left edge, centre or right edge."""
        left = self.elementPositionX - ANCHOR_AT[self.anchor] * self.elementWidth
        return left + {"Left": 0.0, "Center": 0.5, "Right": 1.0}[where] * self.elementWidth

TEMPLATE = "Figure XXX - Area of Interest\nStop The Clog Strategy"
NEW = "Figure 346-140-2056 - Area of Interest\nStop The Clog Strategy"

for anchor in ("BottomLeftCorner", "TopLeftCorner", "LeftMidPoint", "CenterPoint", "TopMidPoint",
               "BottomRightCorner", "RightMidPoint"):
    for align in ("Left", "Center", "Right"):
        el = Text("Figure", TEMPLATE, x=5.0, anchor=anchor, align=align)
        kept = el.edge(align)
        _replace_text(el, NEW)
        check(f"{anchor} + {align}: {align.lower()} point stays put", abs(el.edge(align) - kept) < 1e-9,
              (kept, el.edge(align)))

# The case on the maps: centred text, corner anchor.  Without the fix it drifts right.
el = Text("Figure", TEMPLATE, x=5.0, anchor="BottomLeftCorner", align="Center")
centre = el.edge("Center")
lines = []
_replace_text(el, NEW, lines.append)
check("centred title stays centred", abs(el.edge("Center") - centre) < 1e-9)
check("  and the move is logged", any("moved" in l and "center-aligned" in l for l in lines), lines)
plain = Text("Figure", TEMPLATE, x=5.0, anchor="BottomLeftCorner", align="Center")
plain.text = NEW
check("  (setting .text alone drifts, as on the maps)", plain.edge("Center") - centre > 0.3)

el = Text("Figure", TEMPLATE, x=5.0, cim="dict")
centre = el.edge("Center")
_replace_text(el, NEW)
check("CIM given as plain dicts works too", abs(el.edge("Center") - centre) < 1e-9)

lines = []
el = Text("Figure", TEMPLATE, x=5.0, cim=None)
_replace_text(el, NEW, lines.append)
check("unreadable CIM: text still set, element not moved", el.text == NEW and el.elementPositionX == 5.0)
check("  and logged", any("could not read" in l for l in lines), lines)
lines = []
el = Text("Figure", TEMPLATE, x=5.0, align="Justify")
_replace_text(el, NEW, lines.append)
check("unknown alignment: not moved", el.elementPositionX == 5.0 and el.text == NEW)

lines = []
el = Text("date", "01/01/26", x=9.0, anchor="BottomRightCorner", align="Right")
_replace_text(el, "11/19/26", lines.append)
check("same width: nothing moved or logged", el.elementPositionX == 9.0 and not lines, lines)

# Through _set_text_elements, as the export calls it
figure = Text("Figure", TEMPLATE, x=4.0, anchor="TopLeftCorner", align="Center")
date = Text("date", "1/1/26", x=9.0, anchor="BottomLeftCorner", align="Right")
other = Text("Disclosure", "For Internal Use Only", x=1.0)
layout = types.SimpleNamespace(listElements=lambda kind: [figure, date, other])
centre, right = figure.edge("Center"), date.edge("Right")
_set_text_elements(layout, "152228", "346-140-2056", "11/19/2026")
check("export: figure text set", figure.text.startswith("Figure 346-140-2056 - Area of Interest"))
check("export: figure stays centred", abs(figure.edge("Center") - centre) < 1e-9)
check("export: right-aligned date keeps its right edge", abs(date.edge("Right") - right) < 1e-9)
check("export: other text untouched", other.text == "For Internal Use Only" and other.elementPositionX == 1.0)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)
