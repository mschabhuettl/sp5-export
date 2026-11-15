# Copied from Dispofold

Dispofold commit: `e87bd45906f68ff9fcbfad714da141033c3f87e3`

| Here | In Dispofold |
|---|---|
| `tests/dispofold/dataset.schema.json` | `docs/dataset.schema.json` |
| `tests/dispofold/accept/`, `tests/dispofold/reject/` | `tests/fixtures/contract/accept/`, `tests/fixtures/contract/reject/` |
| `docs/datenformat.md` | `docs/datenformat.md` |

The files are unchanged, except that `dataset.schema.json` also carries the optional person field `shiftMix`
(`$defs.person.properties.shiftMix`, `$defs.shiftMix`), copied from Dispofold's working tree on top of commit
`a22da076a7391d8dae81ce3bcb9328d166040b00` before that change was committed. `tests/contract.py` mirrors the
import validator of the same commit (`DatasetValidator.java`, default rules: `maxDutyHours` 12) plus the rule
`import.badShiftMix` of that working tree (`validMix`). Once Dispofold commits `shiftMix`, take the files from
that commit as below and put its id here.

To update, in a Dispofold checkout:

```sh
C=$(git log -1 --format=%H -- docs/dataset.schema.json tests/fixtures/contract)
git archive "$C" docs/dataset.schema.json docs/datenformat.md tests/fixtures/contract | tar -x -C <empty dir>
```

then copy the files to the places above, put the new commit id here, check `tests/contract.py` against the
validator of that commit and run the tests.
