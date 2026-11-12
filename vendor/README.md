# Vendored libraries

`sp5lib/` is the `sp5lib` package of libopenschichtplaner5 version 1.32.3, the owner's own library under
the MIT License (see `sp5lib/LICENSE`), copied from the owner's repository of that library. The only
change: two documentation links in `sp5lib/orm/README.md` were removed; the package contains no links to
code hosting platforms. The code itself is unchanged.

The exporter uses only `sp5lib.dbf_reader`, `sp5lib.calculations` and `sp5lib.color_utils`, which need
nothing beyond the Python standard library. The tests also use `sp5lib.dbf_writer` (POSIX only, it imports
`fcntl`) to write synthetic SP5 folders.
