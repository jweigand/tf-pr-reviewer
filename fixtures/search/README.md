# Recorded Search API pages

Replay-mode input. These are raw, unmodified GitHub Search API responses, so
replay enters the pipeline upstream of the Connect filter and exercises it for real.

## page-01.json

Recorded 2026-09-11.

    GET https://api.github.com/search/issues
        ?q=is:pr+is:open+language:HCL
        &sort=updated&order=desc&per_page=100&advanced_search=true

Headers: `Authorization: Bearer $GITHUB_TOKEN`, `User-Agent: tf-pr-reviewer`,
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.

`advanced_search=true` is required. GitHub is retiring the legacy issue search
behind that flag.

Composition, and what each filter stage should remove:

| Stage | Count |
|---|---|
| Returned by the API | 100 |
| Bots (`user.type == "Bot"`, all also `[bot]` suffixed) | 66 |
| Drafts, human-authored | 3 |
| Survives the bot and draft filter | 31 |
| Of those, dependency automation under a User account | 15 |
| Genuinely human-authored Terraform PRs | 16 |

Replay reads **one** page, `page-01.json` by default. Recording a second page does
not add it automatically: set `REPLAY_PAGE=/fixtures/search/page-02.json` to replay
that one instead. This is the cost of replay polling on an interval, which it does so
that it runs the same dedupe live does.

Re-record with `scripts/record-fixtures.sh`. The search rate limit is 30 requests
per minute, separate from the 5,000 per hour core limit.
