#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heal stale absolute paths baked into the committed venv after a move.

The bundled venv (runtime/venv312) records the base-python location from when
it was first created. When the project is relocated, venv python refuses to
start ("No Python at '...'"). This rewrites the stale absolute project root in
pyvenv.cfg and the activate scripts to the current PROJ so the build stays
portable (the project can live at any path).

Called by build.bat with PROJ set in the environment.
"""
import os
import re
import sys

proj = os.environ.get("PROJ")
if not proj:
    print("[heal] PROJ env not set; aborting")
    sys.exit(2)

proj = proj.replace("/", "\\")
venv_dir = os.path.join(proj, "runtime", "venv312")

# 旧根 = pyvenv.cfg / activate 中第一个 "\runtime" 之前那段绝对路径
ROOT_RE = re.compile(r"([A-Za-z]:.*?)[\\/]runtime")


def heal_file(path, label):
    if not os.path.isfile(path):
        print("[heal] skip: %s not found" % path)
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = ROOT_RE.search(text)
    if not m:
        print("[heal] %s: no stale absolute root detected, left as-is" % label)
        return
    stale = m.group(1).rstrip("\\/")
    if stale.lower() == proj.lower():
        print("[heal] %s already points to current location" % label)
        return
    new_text = text.replace(stale, proj)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("[heal] %s: '%s' -> '%s'" % (label, stale, proj))


# 1) pyvenv.cfg (home / executable / command)
heal_file(os.path.join(venv_dir, "pyvenv.cfg"), "pyvenv.cfg")

# 2) activate scripts (VIRTUAL_ENV)
for name in ("activate.bat", "activate"):
    heal_file(os.path.join(venv_dir, "Scripts", name), name)
