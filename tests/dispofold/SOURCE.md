# Copied from Dispofold

Dispofold commit: `8eb83fc17ba54ebf4a356a9c7797c0c8186fe11f`

| Here | In Dispofold |
|---|---|
| `tests/dispofold/dataset.schema.json` | `docs/dataset.schema.json` |
| `tests/dispofold/accept/`, `tests/dispofold/reject/` | `tests/fixtures/contract/accept/`, `tests/fixtures/contract/reject/` |
| `docs/datenformat.md` | `docs/datenformat.md` |

The files are unchanged. `tests/contract.py` mirrors the import validator of the same commit
(`DatasetValidator.java`, default rules: `maxDutyHours` 12), including `import.badShiftMix`.

To update, in a Dispofold checkout:

```sh
C=$(git log -1 --format=%H -- docs/dataset.schema.json tests/fixtures/contract)
git archive "$C" docs/dataset.schema.json docs/datenformat.md tests/fixtures/contract | tar -x -C <empty dir>
```

then copy the files to the places above, put the new commit id here, check `tests/contract.py` against the
validator of that commit and run the tests.
