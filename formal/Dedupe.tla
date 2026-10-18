----------------------------- MODULE Dedupe -----------------------------
EXTENDS Naturals, Sequences, FiniteSets

\* t3_done_notify.py:_session_thread/_content_key: three distinct turn keys,
\* two belonging to session 1, one to session 2. Hash collisions are excluded.
CONSTANTS BoundHistory, Capacity, AllowRotation, AllowRollback, RespectDedupe
Sessions == 1..2
Keys == 1..3
Owner(key) == IF key \in {1, 2} THEN 1 ELSE 2
SeqSet(seq) == {seq[i] : i \in 1..Len(seq)}
DropRecord(seq, s) ==
    IF s \notin SeqSet(seq) THEN seq
    ELSE LET position == CHOOSE i \in 1..Len(seq) : seq[i] = s
         IN [i \in 1..(Len(seq) - 1) |->
              IF i < position THEN seq[i] ELSE seq[i + 1]]

\* t3_done_notify.py:poll_local_sessions/transition: absent or retained record.
\* pending is pending_since present; each later poll is past debounce expiry.
EmptyRecord == [present |-> FALSE, active |-> FALSE, pending |-> FALSE,
                completed |-> 0, path |-> 2]

\* t3_done_notify.py:_remember_local_completion: persistent, uncapped identities.
\* Existing keys are NOT moved. BoundHistory=TRUE seeds the old eviction bug.
Remember(seq, key) ==
    IF key \in SeqSet(seq) THEN seq
    ELSE LET added == Append(seq, key)
         IN IF ~BoundHistory \/ Len(added) <= Capacity THEN added
            ELSE SubSeq(added, Len(added) - Capacity + 1, Len(added))

\* t3_done_notify.py:poll_local_sessions, _prune_local_sessions, _track_files:
\* source/path/stat describe stable observed files; indexKey/indexPath mirror
\* _files; records.completed is completed_turn; recent is _local_completed.
\* cache is the per-poll completed set, which survives tombstone eviction and
\* GROWS after each remembered completion within that poll. seen/enqueued are
\* observer-only history. working/payload/remember are the local candidate.
VARIABLES pc, source, path, stat, indexKey, indexPath, records, recordOrder,
          pruneOrder, recent, cache, changed, order, cursor, working, payload,
          remember, seen, enqueued
vars == <<pc, source, path, stat, indexKey, indexPath, records, recordOrder,
          pruneOrder, recent, cache, changed, order, cursor, working, payload,
          remember, seen, enqueued>>

\* t3_done_notify.py:poll_local_sessions: persisted empty discovery baseline;
\* no historical completed files are treated as new on initial installation.
Init ==
    /\ pc = "idle"
    /\ source = [s \in Sessions |-> 0]
    /\ path = [s \in Sessions |-> 0]
    /\ stat = [s \in Sessions |-> 0]
    /\ indexKey = [s \in Sessions |-> 0]
    /\ indexPath = [s \in Sessions |-> 2]
    /\ records = [s \in Sessions |-> EmptyRecord]
    /\ recordOrder = <<>> /\ pruneOrder = <<>>
    /\ recent = <<>> /\ cache = {} /\ changed = {}
    /\ order = <<1, 2>> /\ cursor = 1
    /\ working = EmptyRecord /\ payload = FALSE /\ remember = FALSE
    /\ seen = {} /\ enqueued = [key \in Keys |-> 0]

\* t3_done_notify.py:_discover_observations/_session_thread: external new
\* completed attention turn. Files change only BETWEEN polls in this family.
WriteNewTurn(key) ==
    /\ pc = "idle" /\ key \in Keys \ seen
    /\ source' = [source EXCEPT ![Owner(key)] = key]
    /\ seen' = seen \cup {key}
    /\ UNCHANGED <<pc, path, stat, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, cache, changed, order, cursor, working, payload,
         remember, enqueued>>

\* t3_done_notify.py:_read_stable_session/_signature_changed/_index_entry:
\* touch, inode replacement and same-byte rewrite change stat metadata but
\* retain offset/fingerprint/completion_key. Only two metadata classes needed.
TouchOrRewrite(s) ==
    /\ pc = "idle" /\ s \in Sessions /\ source[s] # 0
    /\ stat' = [stat EXCEPT ![s] = 1 - @]
    /\ UNCHANGED <<pc, source, path, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, cache, changed, order, cursor, working, payload,
         remember, seen, enqueued>>

\* t3_done_notify.py:_prune_local_sessions/_discover_observations: external
\* rename to a new discoverable path; same bytes/key, old path now missing.
RotateSameContent(s) ==
    /\ pc = "idle" /\ AllowRotation /\ s \in Sessions /\ source[s] # 0
    /\ path' = [path EXCEPT ![s] = 1 - @]
    /\ UNCHANGED <<pc, source, stat, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, cache, changed, order, cursor, working, payload,
         remember, seen, enqueued>>

\* t3_done_notify.py:_signature_changed/poll_local_sessions: restore an older
\* real turn of the same session; this is changed content, unlike a touch.
RestoreOldTurn(key) ==
    /\ pc = "idle" /\ AllowRollback /\ key \in seen
    /\ source[Owner(key)] # key
    /\ source' = [source EXCEPT ![Owner(key)] = key]
    /\ UNCHANGED <<pc, path, stat, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, cache, changed, order, cursor, working, payload,
         remember, seen, enqueued>>

\* t3_done_notify.py:_prune_local_sessions: first remove missing index paths.
\* Pruning follows a snapshot of state insertion order; discovery order is
\* independently unspecified. Files are stable during this poll, and no
\* crashes are in this projection.
BeginPoll ==
    /\ pc = "idle" /\ pc' = "prune" /\ cursor' = 1
    /\ order' \in {<<1, 2>>, <<2, 1>>}
    /\ pruneOrder' = recordOrder
    /\ indexKey' = [s \in Sessions |->
         IF indexPath[s] = path[s] THEN indexKey[s] ELSE 0]
    /\ indexPath' = [s \in Sessions |->
         IF indexPath[s] = path[s] THEN indexPath[s] ELSE 2]
    /\ UNCHANGED <<source, path, stat, records, recordOrder, recent, cache,
         changed, working, payload, remember, seen, enqueued>>

\* t3_done_notify.py:_prune_local_sessions: remember a missing-path record's
\* completed_turn, then remove its live record, one record at a time. All
\* pruning finishes BEFORE the poll's completed set is built.
PruneRecord ==
    /\ pc = "prune" /\ cursor \in 1..Len(pruneOrder)
    /\ LET s == pruneOrder[cursor]
           gone == records[s].present /\ records[s].path # path[s]
       IN /\ records' = IF gone THEN [records EXCEPT ![s] = EmptyRecord] ELSE records
          /\ recordOrder' = IF gone THEN DropRecord(recordOrder, s) ELSE recordOrder
          /\ recent' = IF gone /\ records[s].completed # 0
                       THEN Remember(recent, records[s].completed) ELSE recent
    /\ cursor' = cursor + 1
    /\ UNCHANGED <<pc, source, path, stat, indexKey, indexPath, pruneOrder,
         cache, changed, order, working, payload, remember, seen, enqueued>>

\* t3_done_notify.py:_track_files/_signature_changed/_index_entry and the
\* completed = set(...) line of poll_local_sessions. Different turn keys are
\* distinct content signatures; equal keys suppress signature-only changes.
ScanAndCache ==
    /\ pc = "prune" /\ cursor = Len(pruneOrder) + 1
    /\ changed' = {s \in Sessions : source[s] # 0 /\ indexKey[s] # source[s]}
    /\ cache' = SeqSet(recent)
    /\ cursor' = 1 /\ pc' = "observe"
    /\ UNCHANGED <<source, path, stat, indexKey, indexPath, records, recordOrder, pruneOrder,
         recent, order, working, payload, remember, seen, enqueued>>

\* t3_done_notify.py:poll_local_sessions: absent stopped session + unchanged
\* content is skipped; no local record is invented in this branch.
Skip ==
    /\ pc = "observe" /\ cursor \in Sessions
    /\ LET s == order[cursor]
       IN source[s] = 0 \/ (~records[s].present /\ s \notin changed)
    /\ cursor' = cursor + 1
    /\ UNCHANGED <<pc, source, path, stat, indexKey, indexPath, records,
         recordOrder, pruneOrder, recent, cache, changed, order, working,
         payload, remember, seen, enqueued>>

\* t3_done_notify.py:poll_local_sessions/transition: EXACTLY ONE stopped
\* observation, not two polls folded together. First stop starts pending;
\* another poll after debounce expiry fires. Suppressed keys reset pending.
\* RespectDedupe=FALSE mirrors replacing `if already_completed:` with `if False:`.
\* already_completed still controls remembered_key, even in the mutant.
Observe ==
    /\ pc = "observe" /\ cursor \in Sessions
    /\ LET s == order[cursor]
           key == source[s]
           old == records[s]
           known == key \in cache \/ (old.present /\ old.completed = key)
           suppress == RespectDedupe /\ known
           armed == old.active \/ s \in changed
           emit == ~suppress /\ armed /\ old.pending
           pendingNow == ~suppress /\ armed /\ ~old.pending
       IN /\ key # 0 /\ (old.present \/ s \in changed)
          /\ payload' = emit
          /\ remember' = (known \/ emit)
          /\ working' = [present |-> TRUE, active |-> pendingNow,
               pending |-> pendingNow, path |-> path[s],
               completed |-> IF known \/ emit THEN key ELSE old.completed]
    /\ pc' = "commit"
    /\ UNCHANGED <<source, path, stat, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, cache, changed, order, cursor, seen, enqueued>>

\* t3_done_notify.py:_commit_observation/save_state: linearization at the
\* successful os.replace of a per-session candidate. No crashes/I/O failures
\* or concurrent file writes in this projection; Crash.tla splits the actual
\* persistence and delivery boundaries. A healthy webhook drains the queue
\* before the next poll, so cache has no pending outbox keys here.
Commit ==
    /\ pc = "commit"
    /\ LET s == order[cursor]
           key == source[s]
       IN /\ indexKey' = [indexKey EXCEPT ![s] = key]
          /\ indexPath' = [indexPath EXCEPT ![s] = path[s]]
          /\ records' = [records EXCEPT ![s] = working]
          /\ recordOrder' = IF records[s].present THEN recordOrder
                            ELSE Append(recordOrder, s)
          /\ recent' = IF remember THEN Remember(recent, key) ELSE recent
          /\ cache' = IF remember THEN cache \cup {key} ELSE cache
          /\ enqueued' = IF payload THEN
               [enqueued EXCEPT ![key] = IF @ < 2 THEN @ + 1 ELSE 2] ELSE enqueued
    /\ cursor' = cursor + 1 /\ pc' = "observe"
    /\ UNCHANGED <<source, path, stat, pruneOrder, changed,
         order, working, payload, remember, seen>>

\* t3_done_notify.py:poll_local_sessions/flush_outbox: quiescent successful
\* end of poll; local temporaries are forgotten, all queued events accepted.
EndPoll ==
    /\ pc = "observe" /\ cursor = 3 /\ pc' = "idle"
    /\ cache' = {} /\ changed' = {} /\ cursor' = 1
    /\ working' = EmptyRecord /\ payload' = FALSE /\ remember' = FALSE
    /\ UNCHANGED <<source, path, stat, indexKey, indexPath, records, recordOrder,
         pruneOrder, recent, order, seen, enqueued>>

\* t3_done_notify.py:main/poll_local_sessions and filesystem inputs.
Next == (\E key \in Keys : WriteNewTurn(key) \/ RestoreOldTurn(key)) \/
        (\E s \in Sessions : TouchOrRewrite(s) \/ RotateSameContent(s)) \/
        BeginPoll \/ PruneRecord \/ ScanAndCache \/ Skip \/ Observe \/ Commit \/ EndPoll
Spec == Init /\ [][Next]_vars

\* t3_done_notify.py:_commit_observation: the at-most-once conjunct of the
\* requested exactly-once property, counted at durable logical enqueue.
NoDuplicateEnqueue == \A key \in Keys : enqueued[key] <= 1
=============================================================================
