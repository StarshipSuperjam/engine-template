#!/usr/bin/env python3
"""A seeded shipped tool whose docstring cites the local record ZZ-1 by bare identifier.

This file exists only as the negative fixture for engine/check/shipped-local-references: a bare local
reference (from the fixture's declared `ZZ-` vocabulary) in the prose of a file that would ship into a
generated repository, where that identifier resolves to nothing a reader can reach. The floor must flag it.
A synthetic prefix is used deliberately so the seeded token is not itself a real engine decision-record
reference the mechanic's containment scan would report.
"""


def do_work():
    # see ZZ-1 for why a bare local reference dangles in a generated repository
    return None
