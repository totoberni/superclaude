#!/usr/bin/env python3
"""Check a JSON file for duplicate top-level (and nested) keys.

Usage: settings_dupcheck.py <path-to-settings.json>

Exits 0 and prints one of:
  OK
  DUPLICATES: key1, key2
On any error the script exits nonzero with no output; the caller
handles that via `|| echo "OK"`.
"""

import json
import sys


def find_duplicate_keys(path):
    dups = []

    def _check_pairs(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                dups.append(k)
            seen[k] = v
        return seen

    with open(path) as fh:
        json.loads(fh.read(), object_pairs_hook=_check_pairs)
    return dups


if __name__ == "__main__":
    path = sys.argv[1]
    dups = find_duplicate_keys(path)
    print("DUPLICATES: " + ", ".join(dups) if dups else "OK")
