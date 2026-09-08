#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail when the two copies of the nginx site config disagree.

There are two, and only one of them runs:

    frontend/nginx.conf                                   baked into the image
    deployment/charts/frontend/templates/nginx-config.yaml mounted OVER it

The chart mounts its ConfigMap at /etc/nginx/conf.d/default.conf so proxy_pass can
name the environment's own backend (test and prod share a namespace). That mount
masks the image copy entirely, so an edit to frontend/nginx.conf changes nothing
that serves traffic - which is exactly how X-Forwarded-Proto was fixed in one file
and left broken in the other, and how the proxy timeouts existed in only one of them.

Only the directives that must agree are compared: proxy_set_header (what the backend
is told about the request) and add_header (the security headers). proxy_pass, listen
and anything Helm templates are expected to differ and are ignored.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE_COPY = REPO / "frontend" / "nginx.conf"
CHART_COPY = REPO / "deployment" / "charts" / "frontend" / "templates" / "nginx-config.yaml"

# The directives both copies must state identically.
COMPARED = ("proxy_set_header", "add_header")


def strip_helm_comments(text: str) -> str:
    """Remove {{/* ... */}} blocks.

    Not cosmetic: the chart file explains this very check in a Helm comment, and a
    sentence there beginning "proxy_set_header / add_header lines disagree" was read
    as a directive and reported as a difference. A checker that trips over the
    documentation for itself is the noisy kind that gets switched off.
    """
    return re.sub(r"\{\{/\*.*?\*/\}\}", "", text, flags=re.DOTALL)


def directives(text: str) -> set[str]:
    """Every compared directive, normalised to one space between tokens.

    Lines are collected regardless of which block they sit in: a header set in
    `location /` in one file and somewhere else in the other is still the same
    promise, and the point here is that neither copy silently loses one.
    """
    found = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not line.startswith(COMPARED):
            continue
        # Helm-templated values would never match across the two files; there are
        # none in the compared directives today, and this makes that explicit
        # rather than reporting a difference nobody can fix.
        if "{{" in line:
            continue
        found.add(re.sub(r"\s+", " ", line).rstrip(";") + ";")
    return found


def main() -> int:
    for path in (IMAGE_COPY, CHART_COPY):
        if not path.exists():
            print("FAIL - missing " + str(path.relative_to(REPO)))
            return 1

    image = directives(strip_helm_comments(IMAGE_COPY.read_text(encoding="utf-8")))
    chart = directives(strip_helm_comments(CHART_COPY.read_text(encoding="utf-8")))

    if not image or not chart:
        print("FAIL - one copy declares no proxy_set_header/add_header at all; "
              "the parser or the file is wrong")
        return 1

    only_image = sorted(image - chart)
    only_chart = sorted(chart - image)
    if only_image or only_chart:
        print("FAIL - the two nginx configs disagree. The CHART copy is the one that")
        print("       runs in the cluster; the image copy is what local builds get.")
        for line in only_image:
            print("  only in frontend/nginx.conf                  : " + line)
        for line in only_chart:
            print("  only in charts/frontend/.../nginx-config.yaml: " + line)
        return 1

    print("OK - both nginx configs agree on %d proxy_set_header/add_header line(s)"
          % len(image))
    return 0


if __name__ == "__main__":
    sys.exit(main())
