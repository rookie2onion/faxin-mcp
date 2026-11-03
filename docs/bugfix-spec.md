# Reliability repair scope

Source: the user's code review and request to repair its findings using `ask-matt` with autonomous question-and-answer decisions.

## Routing decisions

| Question | Decision |
|---|---|
| Which route fits? | `ask-matt` → `diagnosing-bugs` → `tdd` → `code-review` → `retro`. |
| What is the fixed point? | Upstream commit `368279162dc15ee2f616989cb80a80206d2b0940`, locally represented by `reviewed-3682791`. |
| Which seams are tested? | Exported article lookup/extraction, search-result parsing, detail/outline retrieval, login checks, and the real MCP stdio interface. Replace only external HTTP, browser, time or filesystem boundaries where needed. |
| How should ambiguous login state behave? | Require a visible account control in a browser-rendered search page, using Playwright visibility checks. Raw HTTP markup cannot establish CSS visibility. Report uncertainty instead of treating an anonymous response or error page as authenticated. |
| How should relogin work? | Stop the server, log in through the official page, then restart. Release browser and driver resources on shutdown or launch failure. |
| Does this repair implement main-site case search? | No. Its unsupported state must be explicit and distinct from session expiry. Actual token adaptation needs authenticated evidence. |
| Where is work published? | A repair branch and pull request in `rookie2onion/faxin-mcp`; no automatic merge. |

## Required behavior

1. Article lookup preserves internal zeros and places inserted-article suffixes after `条`.
2. Article extraction preserves cross-references within the text and does not silently truncate the last article.
3. Concurrent detail and outline requests cannot overwrite each other's parsed document. Parsing preserves paragraph boundaries and metadata without executing remote HTML scripts.
4. Anonymous count responses, 404 pages and hidden logout templates do not prove login. A forced failed check invalidates cached success.
5. Detail inputs are valid document gids or HTTPS document URLs at the configured faxin origin. Validate each redirect before sending it. Surface HTTP errors instead of parsing them as content.
6. Unsupported main-site case search is distinct from session expiry; denied case API responses are distinct from an empty search.
7. MCP failures carry `isError=true`; numeric result limits are bounded in tool schemas.
8. Login/profile failures return a nonzero exit code. Stop/login/restart instructions reflect the profile lock. Shutdown releases resources.
9. Search-result group labels are preserved.

Article-boundary refinement: an unspaced article-like prefix can be a heading or a reference. When the selected segment remains ambiguous, report that the article could not be located rather than merging or cutting content. Do not infer a heading solely from an incidental span boundary.

## Validation boundary

Run fast deterministic regressions after each repair and the full offline suite before publication. The suite includes a real stdio client/server handshake.

Anonymous main-site API checks can establish article lookup and detail parsing only. They cannot establish logged-in behavior or paid-library permissions. Record live verification and environment failures explicitly in the README and PR.
