# Repair verification

## Evidence

Command: `python -m unittest discover -s tests -v`

Result: 36 tests pass on Python 3.12 with MCP 1.30.0, Playwright 1.64.1 and lxml 6.1.3. Syntax compilation and `git diff --check` pass. The MCP test starts a real server process and checks initialization, discovery of nine tools, input validation and error flags.

Before repair, focused tests reproduced malformed article queries (`第一百一条`, `第一百之一条`), dropped cross-reference text, a silently cut 2,000-character article, concurrent results both containing the second document, anonymous login success, unrestricted detail URLs, and unsupported or denied capabilities reported as successful MCP results.

Anonymous main-site API requests successfully retrieved Civil Code article 101 and its detail page, including the `现行有效` metadata and exact article selection. No real account was used. Chromium download failed in this environment, so rendered authentication, persistence across browser restarts and paid-library access remain live verification tasks. Offline browser fixtures test the visibility contract; they do not establish account compatibility.

A subsequent anonymous retry returned HTTP 503. The connector surfaced this as an upstream error rather than returning an empty result or parsing it as legal text. The successful earlier probe establishes partial API feasibility, not continuous availability or a fully passing final online run.

## Standards

Independent review identified inert login templates being counted as visible and unspaced article headings being merged into the selected article. Both were repaired and rechecked. Authentication now uses Playwright's rendered visibility checks; ambiguous unspaced article boundaries produce an explicit failure to locate instead of silently returning combined content.

Final targeted review: **0 remaining findings**. No heuristic style findings were raised.

## Spec

Independent review identified paragraph-start references truncating articles, hidden login markup establishing authentication, and permission-denied searches lacking the MCP error flag. Follow-up review also covered native hidden inputs. These cases have regression tests and fixes; exact inserted-article suffixes, CSS-hidden controls, foreign-origin account evidence and browser cleanup are covered too.

Final targeted review: **0 remaining findings**. Authenticated live behavior remains unverified, as required by the validation boundary in `bugfix-spec.md`.

## Retrospective

The original repository had no automated regression command or CI guardrail. The repair adds an offline unittest suite and a GitHub Actions workflow, so future changes can catch legal-text, session and protocol regressions without platform credentials. Keep authenticated live checks separate: absence of a real browser/account must not be presented as a successful login test.

Static HTML cannot establish computed visibility, and plain text cannot always distinguish an unspaced article heading from a reference. The repair uses browser visibility for the former and an explicit uncertainty result for the latter.
