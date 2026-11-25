# Walkthrough with curl

Two users share a team and an outsider looks in. Start the server (`uv run uvicorn app.main:app --port 8765 --timeout-graceful-shutdown 3`, see the [README](../README.md)), then paste the blocks below into one shell in order. Needs `curl`, `jq` and `awk`. The `&` lines are Bob's long polls, run in the background as if in a second terminal; `wait` collects them.

A lock has a 60-second lease. Blocks 3 and 4 start with Alice taking the lock again (a take or a renewal, whichever applies), so a pause of any length between blocks still gives the output shown. A save and a delete must send `If-Match` with the note's version, which the server returns as the `ETag` header of every response that carries one note. Output from a background poll and a foreground call can print in either order. IDs and times differ on each run and are shortened here.

## 1. Accounts, a team, a note

```sh
API=http://localhost:8765
JSON='Content-Type: application/json'
etag() { curl -s -D - -o /dev/null "$@" | tr -d '\r' | awk -F': ' 'tolower($1)=="etag"{print $2}'; }
signup() {
  curl -s -X POST $API/auth/signup -H "$JSON" \
    -d "{\"email\": \"$1@example.com\", \"password\": \"correct horse\", \"name\": \"$1\"}" > /dev/null
  curl -s -X POST $API/auth/login -H "$JSON" \
    -d "{\"email\": \"$1@example.com\", \"password\": \"correct horse\"}" | jq -r .token
}
ALICE=$(signup alice); BOB=$(signup bob); CAROL=$(signup carol)

# Alice creates a team, adds Bob, and writes a note.
TEAM=$(curl -s -X POST $API/teams -H "Authorization: Bearer $ALICE" -H "$JSON" \
  -d '{"name": "Platform"}' | jq -r .id)
curl -s -X POST $API/teams/$TEAM/members -H "Authorization: Bearer $ALICE" -H "$JSON" \
  -d '{"email": "bob@example.com"}' | jq -c '{name, role}'
NOTE=$(curl -s -X POST $API/teams/$TEAM/notes -H "Authorization: Bearer $ALICE" -H "$JSON" \
  -d '{"title": "Runbook", "body": "Step 1"}' | jq -r .id)
N=$API/teams/$TEAM/notes/$NOTE
curl -s $N -H "Authorization: Bearer $BOB" | jq -c '{version, lock, tag}'
etag $N -H "Authorization: Bearer $BOB"
```

```
{"name":"bob","role":"member"}
{"version":1,"lock":null,"tag":"1:"}
"1"
```

The last line is the `ETag`: the note's version as a quoted string.

## 2. Bob watches; Alice takes the lock; Bob is refused

```sh
curl -s -w ' %{http_code} after %{time_total}s\n' "$N/changes?since=1:" -H "Authorization: Bearer $BOB" &
sleep 1
curl -s -X PUT $N/lock -H "Authorization: Bearer $ALICE" | jq -c .
wait
curl -s -X PUT $N/lock -H "Authorization: Bearer $BOB" -w ' %{http_code}\n'
```

```
{"tag":"1:390d...","version":1,"lock":{"user_id":"390d...","user_name":"alice","expires_at":"...","renew_after_seconds":20}} 200 after 1.015625s
{"user_id":"390d...","user_name":"alice","expires_at":"...","renew_after_seconds":20}
{"error":{"code":"note_locked","message":"alice is editing this note.","lock":{"user_id":"390d...","user_name":"alice","expires_at":"...","renew_after_seconds":20}}} 409
```

Bob's poll returned about a second after it started, when Alice's take woke it.

## 3. Alice saves; a stale save and a save without `If-Match` are refused

```sh
curl -s -X PUT $N/lock -H "Authorization: Bearer $ALICE" | jq -c '{user_name}'
V=$(etag $N -H "Authorization: Bearer $ALICE")
TAG=$(curl -s $N -H "Authorization: Bearer $BOB" | jq -r .tag)
curl -s -w ' %{http_code} after %{time_total}s\n' "$N/changes?since=$TAG" -H "Authorization: Bearer $BOB" &
sleep 1
curl -s -X PATCH $N -H "Authorization: Bearer $ALICE" -H "$JSON" -H "If-Match: $V" \
  -d '{"body": "Step 1\nStep 2"}' | jq -c '{version, body}'
wait
# The same If-Match again is now stale (the note is at version 2): 412, carrying the current note.
curl -s -X PATCH $N -H "Authorization: Bearer $ALICE" -H "$JSON" -H "If-Match: $V" \
  -d '{"body": "oops"}' | jq -c '.error | {code, current_version: .note.version}'
# No If-Match at all: 428.
curl -s -X PATCH $N -H "Authorization: Bearer $ALICE" -H "$JSON" \
  -d '{"body": "oops"}' -w '\n%{http_code}\n'
```

```
{"user_name":"alice"}
{"version":2,"body":"Step 1\nStep 2"}
{"tag":"2:390d...","version":2,"lock":{"user_id":"390d...","user_name":"alice","expires_at":"...","renew_after_seconds":20}} 200 after 1.013221s
{"code":"precondition_failed","current_version":2}
{"error":{"code":"precondition_required","message":"Send If-Match with the ETag of the note you read."}}
428
```

The save answers 200 with the new `ETag` (`"2"`). The long poll that Bob started returned when the save landed. A client that gets 412 shows `error.note` to its user, or applies the retry rule in the [API contract](API_CONTRACT.md).

## 4. Renewals

The holder renews every 20 seconds and says whether it has typed since the last renewal. A renewal without `active` is a validation error, and anyone but the holder gets `note_locked`. A holder that sends `active=false` for 10 minutes loses the lock: its next renewal is 409 `lock_idle`.

```sh
curl -s -X PUT "$N/lock?renew=true&active=false" -H "Authorization: Bearer $ALICE" | jq -c '{user_name, renew_after_seconds}'
curl -s -X PUT "$N/lock?renew=true&active=true" -H "Authorization: Bearer $ALICE" | jq -c '{user_name, renew_after_seconds}'
curl -s -X PUT "$N/lock?renew=true" -H "Authorization: Bearer $ALICE" -w ' %{http_code}\n'
curl -s -X PUT "$N/lock?renew=true&active=true" -H "Authorization: Bearer $BOB" -w ' %{http_code}\n'
```

```
{"user_name":"alice","renew_after_seconds":20}
{"user_name":"alice","renew_after_seconds":20}
{"error":{"code":"validation_error","message":"The request is not valid.","details":[{"loc":["query","active"],"message":"Field required with renew=true","type":"missing"}]}} 422
{"error":{"code":"note_locked","message":"alice is editing this note.","lock":{"user_id":"390d...","user_name":"alice","expires_at":"...","renew_after_seconds":20}}} 409
```

## 5. Alice releases; a quiet poll ends with 204

```sh
curl -s -X DELETE $N/lock -H "Authorization: Bearer $ALICE" -w '%{http_code}\n'
TAG=$(curl -s $N -H "Authorization: Bearer $BOB" | jq -r .tag)
echo "tag=$TAG"
curl -s -w '%{http_code} after %{time_total}s\n' "$N/changes?since=$TAG&wait=2" -H "Authorization: Bearer $BOB"
```

```
204
tag=2:
204 after 2.004365s
```

## 6. Paging, search, and an outsider

Alice adds three notes. Bob lists the team's notes two at a time: a page is `items` and `next_cursor`, null on the last page. He passes `next_cursor` back as `cursor`. A search matches case-insensitively, including non-ASCII letters. Carol is not in the team and gets the same 404 as for a team that does not exist.

```sh
for t in "Café menu" "Standup" "Retro"; do
  curl -s -X POST $API/teams/$TEAM/notes -H "Authorization: Bearer $ALICE" -H "$JSON" \
    -d "{\"title\": \"$t\", \"body\": \"\"}" > /dev/null
done
curl -s "$API/teams/$TEAM/notes?limit=2" -H "Authorization: Bearer $BOB" > page1.json
jq -c '{titles: [.items[].title], next_cursor}' page1.json
CUR=$(jq -r .next_cursor page1.json)
curl -s -G "$API/teams/$TEAM/notes" --data-urlencode "limit=2" --data-urlencode "cursor=$CUR" \
  -H "Authorization: Bearer $BOB" | jq -c '{titles: [.items[].title], next_cursor}'
curl -s -G "$API/teams/$TEAM/notes" --data-urlencode "q=CAFÉ" \
  -H "Authorization: Bearer $BOB" | jq -c '{titles: [.items[].title], next_cursor}'
curl -s $N -H "Authorization: Bearer $CAROL" -w ' %{http_code}\n'
curl -s $API/teams/$TEAM/notes -H "Authorization: Bearer $CAROL" -w ' %{http_code}\n'
```

```
{"titles":["Retro","Standup"],"next_cursor":"WyIyMDI2LTExLTI1VDEz..."}
{"titles":["Café menu","Runbook"],"next_cursor":null}
{"titles":["Café menu"],"next_cursor":null}
{"error":{"code":"not_found","message":"Team not found."}} 404
{"error":{"code":"not_found","message":"Team not found."}} 404
```

## 7. Delete

A delete needs `If-Match` like a save: without it, 428; with the current version, 204.

```sh
curl -s -X DELETE $N -H "Authorization: Bearer $ALICE" -w '%{http_code}\n'
V=$(etag $N -H "Authorization: Bearer $ALICE")
echo "v=$V"
curl -s -X DELETE $N -H "Authorization: Bearer $ALICE" -H "If-Match: $V" -w '%{http_code}\n'
curl -s $N -H "Authorization: Bearer $ALICE" -w ' %{http_code}\n'
```

```
{"error":{"code":"precondition_required","message":"Send If-Match with the ETag of the note you read."}}428
v="2"
204
{"error":{"code":"not_found","message":"Note not found."}} 404
```

Run against an empty database. On a second run the signups answer 409 (hidden by `> /dev/null`), the logins still work, and a new team is created.
