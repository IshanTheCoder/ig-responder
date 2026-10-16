# ig-responder

Replies to Instagram comments that contain a keyword by sending the commenter a DM (an Instagram "private reply")
and a short public reply. Instagram's own reply list is the record of who is done, so overlapping runs can't double-send.

This repo is public so GitHub Actions minutes are free. **Nothing private lives here**: the list of Reels, keywords and
DM text (which contain the links you give away) is kept in the GitHub secret `RULES_JSON`. Never commit a real `rules.json`.

## Secrets
**`self_username` in `RULES_JSON` must be exactly the account handle** (it is how the script recognises its own replies; a wrong value means double replies).
 (Settings -> Secrets and variables -> Actions)
- `COMPOSIO_API_KEY` - the Composio project API key for the account with the Instagram connection.
- `RULES_JSON` - same shape as `rules.example.json`, with real media ids, keywords, post times and DM text.

## Running
The workflow only runs when something starts it (`workflow_dispatch`), e.g. a cron-job.org job POSTing to
`https://api.github.com/repos/<owner>/ig-responder/actions/workflows/responder.yml/dispatches` with body
`{"ref":"main","inputs":{"dry_run":"false"}}` and a fine-grained token (Actions: read/write, this repo only).
Manual: Actions tab -> Run workflow (leave dry_run on to send nothing).

## Limits
Up to 600 sends/hour (Meta allows 750), 8 at a time. A Reel stops being served 7 days after `posted_at`
(Instagram only allows private replies for 7 days).

## Tests
`python3 -m unittest test_responder -v`
