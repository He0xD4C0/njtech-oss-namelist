# License policy scanner

`licensecheck.engine` has no Django/database dependency and imports ScanCode
lazily. Web/general workers need not install the `scanner` dependency group.

```python
manifest = compile_policy(policy_git_blobs, empty_staging_directory)
# Caller owns validation/atomic activation, SHA/generation guards and cleanup.
result = scan_candidates(root_license_git_blobs, manifest, activated_directory)
```

Both arguments are mappings of safe, relative POSIX paths to decoded UTF-8 text.
The scanner never checks out or executes a repository. Git blob selection must
exclude symlinks/submodules and enforce byte limits before decoding. The engine
also limits the submitted text and only scans root LICENSE/LICENCE/COPYING/
UNLICENSE/COPYRIGHT variants and files immediately inside root `LICENSES/`.
Precisely: root basenames `license`, `licence`, `copying`, `unlicense`, `copyright`,
case-insensitive, either bare or followed by `.`, `-` or `_` plus any suffix.
`LICENSES/<filename>` is case-insensitive for the directory and one level only.
`NOTICE`, `LICENSEBSD`, `src/LICENSE` and `LICENSES/nested/LICENSE` are not candidates.

Compilation selects policy class by `whitelist/` or `blacklist/`, but never by
filename, extension or an SPDX-like name. ScanCode rules use hashes of normalized
text. The generated `.RULE` files, real compiled `LicenseIndex`, checksum and JSON
manifest are stored wholly inside the supplied directory and survive its rename.
Policy files with contradictory classes or ScanCode-token collisions fail
compilation; the caller must preserve its previously active policy.

Native ScanCode matches/SPDX labels are evidence only. Publication additionally
requires a full policy-index match and exact coverage of **all** retained words
and meaningful symbols, including stopwords, numbers, currency/comparison/
arithmetic/percentage signs and words absent from ScanCode's dictionary. Thus a
high-score MIT fragment or MIT plus an added restriction cannot authorize a
repository. Complete concatenated policy texts and separate LICENSES files are
supported; mixed white/black results go to review. Unclassified/unknown/changed
text goes to review, only blacklisted texts reject, and no candidates reject.

Unicode composition, capitalization, whitespace and routine Markdown heading/
emphasis/inline-code/rule formatting are normalized. Typographic quote
styles are normalized, but other retained punctuation and symbols must match.
ScanCode tokenizes words; a conservative coverage guard also retains all Unicode
punctuation/symbols, so `$1`/`€1`, `<1`/`>1`, `!=1`/`=1` and interval brackets
cannot authorize each other. Isolated copyright notices are ignored
only when ScanCode parses the complete notice without substantive prose. Holder
and year changes then match. Ambiguous notices/inline legal prose remain part of
the text and go to review when changed; this deliberately prefers a review over
erasing a restriction. `All rights reserved` is not erased. A broader comparison
of policy copyright headers is used only to **reject** potential black/white
conflicts, never to grant publication.
Leading plus/minus signs are not discarded as Markdown bullets: an ambiguous
list-vs-operator change belongs in review rather than silently erasing `- $1`.

Compiled pickle artifacts are **server-generated cache**, not an input format.
Keep the cache in server-owned `WORK_ROOT/license-index`, shared by scan workers;
never fetch pickle artifacts from policy repositories or accept user uploads.
Runtime version, artifact hash and full manifest are checked before loading.
Upgrade ScanCode/engine by recompiling policies before running audits.

Native identification uses its own persistent ScanCode cache, configured by
`SCANCODE_CACHE` and `SCANCODE_LICENSE_INDEX_CACHE`. Initialization is serialized
by a process-local thread lock plus a shared file lock; subsequent files reuse
the loaded cache. Never pass `only_builtin=True`/`force=True` per candidate:
ScanCode 32.5's former flag removes its index directory before taking its lock,
breaking cold non-root starts and concurrent workers. The engine calls the
non-destructive default loader and refuses a cache containing additional
directory/plugin rules. Dynamic policy indices remain separate and never
modify this native cache. Keep the native cache dedicated to these workers.

`CandidateValidationError(ValueError)` identifies deterministic invalid input
(unsafe paths, file/text limits, binary NUL or invalid UTF-8). Scanner/runtime
and compiled-policy errors are different exceptions: do not catch all
`ValueError` as a source-side eligibility failure. Internal non-text map entries
remain `TypeError`.

## Real scanner validation

The locked ScanCode 32.5.0 archive binaries do not support macOS ARM64. Use a Linux
amd64 scanner container (emulation is sufficient for local proof). Python 3.12
plus `uv sync --frozen --group scanner` is required. Debian slim needs `libgomp1`,
`libxml2`, `libzstd1`, `libbz2-1.0`, `liblzma5` and `zlib1g`; missing native loaders
raise a sanitized `ScannerUnavailable`, not a fallback/fake result.

With the project test settings in place:

```sh
uv run --frozen --group scanner pytest -m scancode tests/test_licensecheck.py
```

The independent engine can also be checked without loading Django settings:

```sh
PYTHONPATH=. python -m pytest -c /dev/null -p no:django \
  -o markers=scancode -q tests/test_licensecheck.py
```

These tests invoke the real installed detector and compiled dynamic index, not
mocks. Web-only developer environments may skip the scanner-marked module when
the optional dependency is absent. Release validation must install the group
and require the tests to execute, not treat a skipped suite as a scan proof.

ScanCode index/rule APIs follow the pinned version. See the official
[reindex documentation](https://scancode-toolkit.readthedocs.io/en/stable/reference/scancode-cli/cli-scancode-reindex-licenses.html)
and [rule documentation](https://scancode-toolkit.readthedocs.io/en/stable/how-to-guides/how-to-add-new-license-detection-rule.html).
