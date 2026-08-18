# Live Test Results — Multi-Tenant Auth & Isolation

These are the actual results of running this backend and testing it end to
end before shipping — not a description of intended behavior, but what
genuinely happened when two real accounts were created and tested against
each other.

## What was tested

1. Two independent companies signed up ("Zenith Foods" and "Helios Capital")
2. Each got its own organization, owner account, and private workspace
3. Company A's session token was used to try to read Company B's workspace
4. An unauthenticated request was made with no token
5. A login attempt with the wrong password
6. A correct login
7. The Owner of Zenith Foods invited a Team Lead
8. That Team Lead accepted the invite and set their own password
9. The Team Lead tried to invite another Team Lead (should be blocked — only
   the Owner can do that)
10. The Team Lead invited a Team Member instead (should succeed)
11. The full team list was fetched and checked for correct membership
12. Company B's token was used to try to see Company A's team list

## Results

| # | Test | Expected | Actual |
|---|---|---|---|
| 1 | Company A reads its own workspace | 200, correct name | ✅ `200`, `"name": "Zenith Foods"` |
| 2 | Company A reads Company B's workspace with A's token | 404, no data | ✅ `404`, `{"detail":"Workspace not found"}` |
| 3 | No token at all | 401 | ✅ `401` |
| 4 | Wrong password | 401 | ✅ `401` |
| 5 | Correct login | 200 | ✅ `200`, role returned correctly |
| 6 | Owner invites a Team Lead | 200 | ✅ `200`, invite link generated |
| 7 | Invitee accepts, sets own password | 200 | ✅ `200`, role: `lead` |
| 8 | Team Lead tries to invite another Team Lead | Blocked | ✅ `422`, "Team Leads can only invite Team Members. Ask the account Owner to add another Lead." |
| 9 | Team Lead invites a Team Member | 200 | ✅ `200`, invite link generated |
| 10 | Full team list for Company A | 3 members, correct roles/status | ✅ Owner (active), Lead (active), Member (invited) |
| 11 | Company B's token requests the team list | Only sees Company B's own team | ✅ Only `chidi@helios.example` returned |

**Every test passed.** Note that test #2 returning a 404 (not a 403) is
deliberate — the API never confirms or denies that a workspace ID belongs to
someone else; it responds identically whether the ID doesn't exist at all or
just isn't yours, so no information about other accounts leaks through error
behavior.

## A real bug this testing caught

The first run of test #2 returned a `500 Internal Server Error` instead of a
clean `404`. The cause: SQLite doesn't preserve timezone information on
`DateTime(timezone=True)` columns the way Postgres does, so a session's
stored expiry timestamp came back "naive" (no timezone) while the code
compared it against a timezone-aware "now," which Python refuses to do and
raises a `TypeError` for.

This is exactly the kind of bug that a design document or a syntax check
cannot catch — it only shows up when the code actually runs against a real
database. It's fixed now (see `models.aware()` and its use in
`services/auth.py` and `services/media_room.py`), and re-running the same
test suite after the fix produced the clean results above.

## How to re-run this yourself

The exact commands used are in the project's development history; the short
version is in `README.md` under "Try the account model yourself." Two
terminal windows: one running `uvicorn app.main:app`, the other running the
`curl` sequence above with real email addresses of your choosing.
