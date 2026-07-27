# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
#
# embed_ccfull.py — régénère le blob _CCFULL_SO_B64 de script.py depuis tools/cc_full.so.
# Workflow après modification de cc_full.c :
#   1. builder le .so sur un hôte Debian trixie x86_64 (sh tools/build_ccfull.sh), le valider au
#      banc (tools/equiv_ccfull.py = C ≡ numpy à ≤2 LSB ; tools/bench_ccfull.py = perf) ;
#   2. python3 tools/embed_ccfull.py   → réécrit la section entre les marqueurs dans script.py.
# Le blob est du base64 pur (aucune accolade) → sans danger pour le template str.format.
import base64, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "script.py")
SO = os.path.join(HERE, "cc_full.so")

blob = base64.b64encode(open(SO, "rb").read()).decode()
wrapped = "\n".join(blob[i:i + 96] for i in range(0, len(blob), 96))

src = open(SCRIPT).read()
pat = re.compile(r'(_CCFULL_SO_B64 = """\n).*?("""  # fin _CCFULL_SO_B64)', re.S)
new, n = pat.subn(lambda mo: mo.group(1) + wrapped + "\n" + mo.group(2), src)
if n != 1:
    sys.exit("marqueurs _CCFULL_SO_B64 introuvables dans script.py")
open(SCRIPT, "w").write(new)
print(f"blob injecté : {len(blob)} caractères base64 ({os.path.getsize(SO)} octets .so)")
