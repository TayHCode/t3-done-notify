------------------------------ MODULE Crash ------------------------------
EXTENDS Naturals, FiniteSets, TLC

\* t3_done_notify.py:poll_local_sessions: N newly completed, attention-worthy
\* sessions appear after a successfully persisted empty discovery baseline.
\* Their transcripts remain readable and unchanged throughout this model.
CONSTANTS N, MaxCrashes, MaxFailures, DropQueue, BatchIndex, StableIds
Sessions == 1..N

\* t3_done_notify.py:load_state, _track_files, transition, _commit_observation:
\* records 0 = absent, 1 = active/pending debounce, 2 = completed_turn retained.
\* All polls after the first occur after the debounce threshold. Two stopped
\* observations are required even when debounce_seconds = 0.
Empty == [files |-> {}, records |-> [s \in Sessions |-> 0], outbox |-> {}]

VARIABLES pc, disk, memory, candidate, temporary, changed, session,
          record, payload, returnTo, remaining, keep, successful, sending, requestId,
          attempt, crashes, failures, received, identities, daybook, committed
vars == <<pc, disk, memory, candidate, temporary, changed, session,
          record, payload, returnTo, remaining, keep, successful, sending, requestId,
          attempt, crashes, failures, received, identities, daybook, committed>>

\* t3_done_notify.py:poll_local_sessions, deliver, append_daybook. received,
\* daybook and committed are observer-only history; never consulted by code.
Init ==
    /\ pc = "scan"
    /\ disk = Empty /\ memory = Empty /\ candidate = Empty /\ temporary = Empty
    /\ changed = {} /\ session = 1 /\ record = 0 /\ payload = FALSE
    /\ returnTo = "observe" /\ remaining = {} /\ keep = {}
    /\ successful = FALSE /\ sending = 1 /\ requestId = 1 /\ attempt = 1
    /\ crashes = 0 /\ failures = 0
    /\ received = [s \in Sessions |-> 0]
    /\ identities = [s \in Sessions |-> {}]
    /\ daybook = [s \in Sessions |-> 0]
    /\ committed = {}

\* t3_done_notify.py:_track_files: stage signatures without publishing them.
\* BatchIndex=TRUE seeds the old whole-batch publication bug. Content-key equality
\* reduces _signature_changed/_index_entry to membership for fixed files.
Scan ==
    /\ pc = "scan"
    /\ changed' = Sessions \ memory.files
    /\ memory' = IF BatchIndex THEN [memory EXCEPT !.files = Sessions] ELSE memory
    /\ session' = 1 /\ pc' = "observe"
    /\ UNCHANGED <<disk, candidate, temporary, record, payload, returnTo,
         remaining, keep, successful, sending, requestId, attempt, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:poll_local_sessions: first-seen stopped and unchanged
\* files are skipped. No in-memory record is fabricated on this path.
Skip ==
    /\ pc = "observe" /\ session \in Sessions
    /\ memory.records[session] = 0 /\ session \notin changed
    /\ session' = session + 1
    /\ UNCHANGED <<pc, disk, memory, candidate, temporary, changed, record,
         payload, returnTo, remaining, keep, successful, sending, requestId, attempt,
         crashes, failures, received, identities, daybook, committed>>

\* t3_done_notify.py:poll_local_sessions, transition: pure local computation
\* on a deepcopy. A crash during it has the same disk result as before it.
Observe ==
    /\ pc = "observe" /\ session \in Sessions
    /\ (memory.records[session] # 0 \/ session \in changed)
    /\ record' = IF memory.records[session] = 0 THEN 1 ELSE 2
    /\ payload' = (memory.records[session] = 1)
    /\ pc' = "daybook"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         returnTo, remaining, keep, successful, sending, requestId, attempt, crashes,
         failures, received, identities, daybook, committed>>

\* t3_done_notify.py:_commit_observation, append_daybook: successful close
\* of one append. Partial/torn daybook bytes are outside the property; a
\* crash inside append is equivalent for notification recovery to before it.
AppendDaybook ==
    /\ pc = "daybook"
    /\ daybook' = IF payload THEN
         [daybook EXCEPT ![session] = IF @ < 2 THEN @ + 1 ELSE 2] ELSE daybook
    /\ pc' = "build"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, remaining, keep, successful, sending, requestId,
         attempt, crashes, failures, received, identities, committed>>

\* t3_done_notify.py:_commit_observation: candidate records and queue are
\* prepared together, before save_state. DropQueue mirrors the real-code
\* mutation deleting candidate["_outbox"].append(queued).
BuildObservation ==
    /\ pc = "build"
    /\ candidate' = [memory EXCEPT
         !.files = @ \cup {session},
         !.records[session] = record,
         !.outbox = IF payload /\ ~DropQueue THEN @ \cup {session} ELSE @]
    /\ returnTo' = "observe" /\ pc' = "write-temp"
    /\ UNCHANGED <<disk, memory, temporary, changed, session, record, payload,
         remaining, keep, successful, sending, requestId, attempt, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:save_state: json.dump, flush, file fsync all finish in
\* the private temporary file. Partial temporary files are never loaded.
WriteTemp ==
    /\ pc = "write-temp" /\ temporary' = candidate /\ pc' = "replace"
    /\ UNCHANGED <<disk, memory, candidate, changed, session, record, payload,
         returnTo, remaining, keep, successful, sending, requestId, attempt, crashes,
         failures, received, identities, daybook, committed>>

\* t3_done_notify.py:save_state: the actual atomic filesystem operation is
\* os.replace. Assumes a process crash, not power loss or a second writer.
Replace ==
    /\ pc = "replace" /\ disk' = temporary /\ pc' = "publish"
    /\ committed' = committed \cup
         {s \in Sessions : temporary.records[s] = 2}
    /\ UNCHANGED <<memory, candidate, temporary, changed, session, record,
         payload, returnTo, remaining, keep, successful, sending, requestId, attempt,
         crashes, failures, received, identities, daybook>>

\* t3_done_notify.py:_commit_observation / flush_outbox: state.clear/update
\* publish the saved candidate. Intermediate RAM-only states vanish on crash.
Publish ==
    /\ pc = "publish" /\ memory' = candidate /\ pc' = returnTo
    /\ session' = IF returnTo = "observe" THEN session + 1 ELSE session
    /\ UNCHANGED <<disk, candidate, temporary, changed, record, payload,
         returnTo, remaining, keep, successful, sending, requestId, attempt, crashes,
         failures, received, identities, daybook, committed>>

\* t3_done_notify.py:flush_outbox: candidate/keep is prepared in RAM; a
\* successful item is acknowledged on disk only after the entire batch.
StartFlush ==
    /\ pc = "observe" /\ session = N + 1
    /\ remaining' = memory.outbox /\ keep' = {} /\ successful' = FALSE
    /\ pc' = "pick"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, sending, requestId, attempt, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:flush_outbox: list order is arbitrary in the abstraction;
\* each pending event is selected once per batch (no concurrent sender).
Pick ==
    /\ pc = "pick" /\ remaining # {}
    /\ \E s \in remaining :
         /\ sending' = s
         /\ requestId' = IF StableIds \/ received[s] = 0 THEN 1 ELSE 2
    /\ attempt' = 1 /\ pc' = "send"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, remaining, keep, successful, crashes,
         failures, received, identities, daybook, committed>>

\* t3_done_notify.py:deliver: remote side effect precedes receipt of response.
\* Saturation at two retains the distinction needed for duplicate detection.
\* event/deliver/flush_outbox preserve one event_id and Idempotency-Key for a
\* logical event; identities records keys received by a deduping receiver.
\* StableIds=FALSE mirrors changing that header on every deliver call.
HttpAccept ==
    /\ pc = "send" /\ pc' = "response"
    /\ received' = [received EXCEPT ![sending] = IF @ < 2 THEN @ + 1 ELSE 2]
    /\ identities' = [identities EXCEPT ![sending] =
         @ \cup {requestId}]
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, remaining, keep, successful, sending, requestId,
         attempt, crashes, failures, daybook, committed>>

\* t3_done_notify.py:deliver: failed request or lost response, including an
\* accepted request followed by a timeout. Fair recovery is bounded by faults.
HttpFailure ==
    /\ pc \in {"send", "response"} /\ failures < MaxFailures
    /\ failures' = failures + 1 /\ pc' = "retry"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, remaining, keep, successful, sending, requestId,
         attempt, crashes, received, identities, daybook, committed>>

\* t3_done_notify.py:deliver, flush_outbox: only a 2xx response returns True.
Http2xx ==
    /\ pc = "response" /\ remaining' = remaining \ {sending}
    /\ successful' = TRUE /\ pc' = "pick"
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, keep, sending, requestId, attempt, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:deliver: len(RETRY_DELAYS)+1 = five HTTP attempts. Retry
\* delays do not change durable state; exhaustion retains the item in keep.
Retry ==
    /\ pc = "retry"
    /\ IF attempt < 5 THEN
         /\ attempt' = attempt + 1 /\ pc' = "send"
         /\ UNCHANGED <<remaining, keep>>
       ELSE
         /\ keep' = keep \cup {sending} /\ remaining' = remaining \ {sending}
         /\ pc' = "pick" /\ UNCHANGED attempt
    /\ UNCHANGED <<disk, memory, candidate, temporary, changed, session,
         record, payload, returnTo, successful, sending, requestId, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:flush_outbox: failure-only batches do not save; mixed
\* batches persist keep using the same temporary-file/replace protocol.
FinishFlush ==
    /\ pc = "pick" /\ remaining = {}
    /\ candidate' = [memory EXCEPT !.outbox = keep]
    /\ returnTo' = "scan"
    /\ pc' = IF successful THEN "write-temp" ELSE "scan"
    /\ UNCHANGED <<disk, memory, temporary, changed, session, record, payload,
         remaining, keep, successful, sending, requestId, attempt, crashes, failures,
         received, identities, daybook, committed>>

\* t3_done_notify.py:main/load_state: process termination can occur between
\* any modeled boundaries. RAM and temporary files are not recovery inputs.
CrashProcess ==
    /\ pc # "restart" /\ crashes < MaxCrashes
    /\ crashes' = crashes + 1 /\ pc' = "restart"
    /\ memory' = Empty /\ candidate' = Empty /\ temporary' = Empty
    /\ changed' = {} /\ session' = 1 /\ record' = 0 /\ payload' = FALSE
    /\ remaining' = {} /\ keep' = {} /\ successful' = FALSE
    /\ returnTo' = "observe" /\ sending' = 1 /\ requestId' = 1 /\ attempt' = 1
    /\ UNCHANGED <<disk, failures, received, identities, daybook, committed>>

\* t3_done_notify.py:load_state: retained transcripts mean pruning is a no-op.
Restart ==
    /\ pc = "restart" /\ memory' = disk /\ pc' = "scan"
    /\ UNCHANGED <<disk, candidate, temporary, changed, session, record,
         payload, returnTo, remaining, keep, successful, sending, requestId, attempt,
         crashes, failures, received, identities, daybook, committed>>

\* t3_done_notify.py:main polling loop, under eventual scheduling and recovery.
Step == Scan \/ Skip \/ Observe \/ AppendDaybook \/ BuildObservation \/
        WriteTemp \/ Replace \/ Publish \/ StartFlush \/ Pick \/ HttpAccept \/
        HttpFailure \/ Http2xx \/ Retry \/ FinishFlush \/ Restart
Next == Step \/ CrashProcess
Spec == Init /\ [][Next]_vars /\ WF_vars(Step)

\* t3_done_notify.py:poll_local_sessions/_commit_observation: if a process
\* crashes, an observed fixed completion must retain a way to be rediscovered,
\* be pending in a record, be queued, or have reached the receiver.
NoStrandedCompletion == pc = "restart" =>
    \A s \in Sessions : received[s] > 0 \/ s \in disk.outbox \/
        disk.records[s] = 1 \/ s \notin disk.files

\* t3_done_notify.py:_commit_observation/flush_outbox: durable done record
\* must never acknowledge an undelivered attention event without its outbox.
DurableOutbox == \A s \in committed : s \in disk.outbox \/ received[s] > 0

\* t3_done_notify.py:poll_local_sessions/deliver: full end-to-end no-loss
\* obligation for the fixed new completions; finite faults, fair polling.
EventuallyDelivered == <> (\A s \in Sessions : received[s] > 0)

\* t3_done_notify.py:deliver: strict physical exactly-once is deliberately
\* checked as a separate finding; timeout/crash retries can repeat a POST.
AtMostOneReceipt == \A s \in Sessions : received[s] <= 1
\* t3_done_notify.py:event/deliver/flush_outbox: repeated physical receipts
\* carry the same identity, so a receiver that durably dedupes has one effect.
StableEventIdentity == \A s \in Sessions : Cardinality(identities[s]) <= 1
AtMostOneEffect == \A s \in Sessions : Cardinality(identities[s]) <= 1
=============================================================================
