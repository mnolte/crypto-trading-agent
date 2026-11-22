---
item: 029
type: ops
title: OpenAI SDK 3.x httpx2 migration
status: merged
issue: 54
kickoff: —
spec_prs: []
build_prs: [55]
---

# 029 · ops · OpenAI SDK 3.x httpx2 migration

openai 3.x moved its HTTP layer to `httpx2` (OpenAI's httpx fork), so the
`httpx.AsyncClient` capture transport in `OpenAIResponsesClient` and the
`httpx.Request/Response` objects in `test_openai_client.py` no longer
type-check — dependabot PR #33 failed mypy in CI. The client, its transport
and the tests now use `httpx2`; `httpx` stays for the app's own clients
(Bitvavo, sources). `httpx2` is a declared dependency and `openai` allows
3.x. Byte-exact capture of model calls is unchanged (hard rule 7). Note:
httpx2 verifies TLS against the OS trust store instead of certifi.
