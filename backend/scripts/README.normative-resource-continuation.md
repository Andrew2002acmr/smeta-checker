# Native resource-continuation build utility

This is the existing offline continuation mechanism relocated from
`smeta-checker-local/comprehensive-road-kb/tools/resource_continuation.py` into
the maintained backend checkout. `UPSTREAM` records its original version and
SHA256. Dependencies are Python standard library and the existing `full` group
pin `pymupdf==2.0`; no installation or network service is needed in this venv.

The old offline entry point is `comprehensive-road-kb/assemble.py:build`, which
calls `resource_compiler.extract` and then the continuation helper. Those external
files are not changed. The backend model worker only reads pinned SQLite resources;
this utility does not run in the model worker or project-PDF preparation queue.

The new targeted, read-only entry point is:

```powershell
.venv/Scripts/python.exe -X utf8 -m scripts.prepare_resource_continuation `
  --root <knowledge-root> --database-relative <pinned-relative.sqlite> `
  --database-sha256 <sha256> --source-id <source-id> `
  --header-page 12 --page 13 --norm-code 27-02-010-02 `
  --resource-code 04.1.02.05-0006 --output-json <new-diagnostic.json>
```

It verifies the explicit database and primary PDF hashes, reads SQLite with
`mode=ro&immutable=1`, and exports candidates outside the knowledge root. It does
not change a database, CURRENT, review state or engineering conclusion. The output
is `native_candidate_requires_visual_review`, never an automatic reviewed manifest.
Existing output files cannot be overwritten.

Shared code/name/unit columns must retain their geometry within 0.75 PDF points.
A numeric column inherits only its own literal norm header and only when both of
its current edges remain within the same 0.75-point tolerance. Other shifted
numeric columns remain excluded; no number-based remapping occurs. Vertically
merged first descriptor cells may contain several individually valid resource-code
lines. Arbitrary text, new definitions, unit ambiguity, raw duplicate resource codes
and conflicting existing resource identities remain blocked.

For the reviewed primary GESN27 pages12–13, columns010-01/02/03 remain stable;
010-04/05 shift and remain excluded. Target010-02 contains literal resource
04.1.02.05-0006, B15(M200), 5.9m3. This is literal extraction evidence, not proof
of norm applicability, F/W requirements, coefficients, project quantities or losses.

After independent visual review, an explicitly new inactive-release staging
transaction can call `normative_resource_continuation.apply_reviewed(db, root,
review)` with `status=visually_reviewed_candidate_not_published`. That existing API
rebuilds all approved rows from the primary PDF and compares them exactly before
insertion. A v2 manifest is required: old v1 reviews are not silently reapproved
under the changed parser. Preserve the prior `resource_continuations` metadata as
history in the staged release alongside the new proof (the original API updates
that one metadata key). Existing resource rows must not be overwritten. Clone the
pinned prior release first and verify CURRENT/old release hashes before and after.
Do not invoke the external standalone base compiler against the active root: it
writes its root's CURRENT pointer.
