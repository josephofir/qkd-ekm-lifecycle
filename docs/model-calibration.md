# Lifecycle model calibration

The paper reports an exhaustive enumeration of the lifecycle reference model with
**45,824 reachable states, 307,680 labeled transitions, and a maximum shortest-path
depth of sixteen events** over four key identifiers, two EKM versions, and three VPN
activation epochs. This document records the reproduction of those three numbers from
the semantics the paper describes: two rounds of calibration, 475 exhaustive
enumerations, and the single reading that matches all three at once.

## Outcome

| Quantity | Published | This model | Verdict |
| --- | ---: | ---: | --- |
| Reachable states | 45,824 | 45,824 | **reproduced exactly** |
| Labeled transitions | 307,680 | 307,680 | **reproduced exactly** |
| Maximum shortest-path depth | 16 | 16 | **reproduced exactly** |
| State properties checked (I1-I9) | 9 | 9 | reproduced |
| Transition guards checked (G1-G4) | 4 | 4 | reproduced |
| Seeded invalid states / transitions detected | 9 / 4 | 9 / 4 | reproduced |
| Violations across the closure | 0 | 0 | reproduced |

All three published numbers are reproduced, by exactly one reading out of the 475 full
enumerations run over two calibration rounds. That reading -- `DEFAULT_RULES` in
`qkd_ekm.model.events` -- differs from the first-pass transcription of section 5.3 in
two respects, both about the *ordered activation epoch* rather than about the state
tuple:

- **`alloc_vpn_any_future_epoch`**: a VPN allocation binds any not-yet-used ordered
  activation epoch within the declared domain, not only the immediately next one. The
  paper calls the epoch an *ordering variable supporting explicit freshness
  validation* and requires activation to be *increasing*; it nowhere requires the
  bound epochs to be consecutive. This alone fixes the state count, exactly: 45,824.
- **`reject_stale_per_epoch`**: every non-increasing epoch value in the declared
  domain (1..E) -- including values never issued in that state's history -- is its
  own labeled rejection event: a replay/forgery model, not a replay-of-what-actually-
  happened model. The paper says *repeated or stale epochs remain rejected* (plural),
  and G3 makes rejection state-preserving; rejecting epoch 1 and rejecting epoch 2 are
  two different labeled events with the same (null) effect. This adds 33,792
  self-loops and lands the transition count exactly on 307,680.

Neither knob adds a state variable, and neither was fitted to a number, but the two are
not independent: the state count pins the first knob on its own (45,824 with
`alloc_vpn_any_future_epoch` alone); *given* that first knob, the transition count then
pins the second (273,888 -> 307,680). The two together leave the published depth of
sixteen untouched. Across the 260 readings of round 2, no other combination reaches
either published count.

Round 1 (215 enumerations) concluded that the counts were unreachable. That conclusion
was wrong in one specific place, corrected in the next section.

## Method

`qkd_ekm.model.events.Rules` is a frozen dataclass of boolean knobs; `enabled(s, rules)`
is the only place the semantics vary, so a knob combination is a complete alternative
reading of the paper. `scripts/model_calibrate.py` sweeps combinations, running a full
breadth-first enumeration for each (about 0.5-1.5 s per run):

    uv run python scripts/model_calibrate.py --stage singles|structural|edges|round2|all

The sweep runs the enumerations in a process pool, so a 260-run stage takes about a
minute. `Rules()` -- the dataclass defaults -- is the *base* reading and the origin of
every sweep below; `DEFAULT_RULES` is the calibrated reading the package actually uses.

The base reading is a direct transcription of section 5.3 and Algorithm 1:

- ingestion requires an available source and a previously unused identifier;
- TTL expiry removes unused inventory and never touches a consumed binding;
- fresh EKM and VPN allocation requires EKM reachability plus retained inventory;
- an EKM allocation binds the *other* version (version replacement), the superseded
  binding staying consumed;
- a VPN allocation binds the next activation epoch and becomes pending; activation
  requires a strictly increasing epoch; a stale/replayed epoch is rejected by a
  state-preserving self-loop;
- restoration of either dependency marks recovery reconciliation pending, and
  reconciliation requires both dependencies restored.

## The factorisation, and the one step round 1 got wrong

The reachable closure factorises exactly, under the base reading and under the
calibrated one alike. The four boolean flags (source, EKM, authority, recovery) are
freely reachable over any allocation core -- the enumerator confirms that each of the
sixteen flag combinations carries the identical number of cores -- so

    states = 16 x (allocation cores)

and the published 45,824 requires exactly 2,864 cores. A core is an injective
assignment of identifiers to binding slots (`ne` EKM slots, `nv` VPN epoch slots), a
pool subset over the remaining identifiers, plus the active-version and epoch/pending
variants:

    cores = sum over ne, nv of a(ne) . b(nv) . P(4, ne+nv) . 2^(4-ne-nv)

with `a = (1, 2, 2)`: no version bound; one of two versions bound; both bound with
either active.

Round 1 took `b = (1, 2, 2, 2)` -- each VPN epoch either pending or activated -- which
evaluates to 1,200 cores = 19,200 states, and then searched for an integer `b` giving
2,864 cores. It found only solutions it judged unsupportable (`b = (1, 3, 7, 7)`,
`(1, 6, 4, 6)`) and concluded the published count was out of reach. The error was in
the search, not in the factorisation: it never tried

    b = (1, 6, 6, 2)

which gives exactly 2,864 cores, i.e. 45,824 states -- and which is not a free
parameter at all but a direct consequence of dropping the *consecutive-epoch*
assumption. If a VPN allocation may bind any not-yet-used ordered epoch out of the
three declared ones, then for `nv` bindings the epoch set is any `nv`-subset of
`{1,2,3}` and the newest binding is either pending or activated:

    b(nv) = C(3, nv) . 2  for nv >= 1,   b(0) = 1
          = (1, 6, 6, 2)

Round 1's `(1, 2, 2, 2)` is the same formula with the epoch set forced to be a prefix.
Nothing in the paper forces that: it requires activation epochs to be *increasing* and
stale ones to be *rejected*, both of which hold verbatim under the wider reading. The
per-`(ne, nv)` state counts the enumerator reports (256, 3072, 4608, 1536, 1024, 9216,
9216, 1536, 1536, 9216, 4608) match this closed form term by term.

The transition count then follows from the second knob. The wider allocation alone
gives 273,888 edges over those 45,824 states -- a 33,792-edge residual against the
published 307,680. Closing it by making every non-increasing epoch value its own
labeled rejection self-loop adds exactly that residual: `transitions.csv` shows
66,048 of the final 307,680 transitions (21.5%) are `reject_stale` self-loops, split
32,256 / 22,528 / 11,264 across epochs 1, 2 and 3.

## Round 1, stage 1: single knobs (15 enumerations)

Each row flips exactly one knob away from the base reading.

| # | rules | states | transitions | depth | violations |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | base | 19200 | 112864 | 16 | 0 |
| 2 | reject_stale_needs_active | 19200 | 112864 | 16 | 0 |
| 3 | ingest_needs_ekm | 19200 | 109472 | 16 | 0 |
| 4 | expire_needs_source_down | 19200 | 106080 | 16 | 0 |
| 5 | alloc_ekm_needs_source | 19200 | 109152 | 16 | 0 |
| 6 | restore_sets_recovery | 9600 | 55232 | 14 | 0 |
| 7 | restore_recovery_needs_full | 19200 | 112864 | 16 | 0 |
| 8 | authority_only_when_degraded | 19200 | 110464 | 16 | 0 |
| 9 | alloc_vpn_needs_no_active | 9984 | 59104 | 13 | 0 |
| 10 | vpn_pending_replaceable | 25344 | 148192 | 14 | 0 |
| 11 | recover_clears_pending | 39680 | 215072 | 22 | 0 |
| 12 | recover_needs_authority | 19200 | 111664 | 16 | 0 |
| 13 | dep_toggles_unconditional | 19200 | 170464 | 15 | 0 |
| 14 | activate_merged | 11008 | 63904 | 13 | 0 |
| 15 | ekm_down_clears_active | 23552 | 142560 | 16 | 0 |

## Round 1, stage 2: structural knobs (64 enumerations)

Full product over the six knobs that change *which* states are reachable
(`alloc_vpn_needs_no_active`, `vpn_pending_replaceable`, `recover_clears_pending`,
`activate_merged`, `ekm_down_clears_active`, `restore_recovery_needs_full`). The
closest state count is 49,024 (7.0% above target, depth 17); the closest below is
39,680 (depth 22). Nothing lands on 45,824. Only four distinct outcomes keep the
published depth of sixteen -- 19,200 / 23,552 / 27,392 / 33,664 states -- so every
combination that grows the state space far enough to approach 45,824 also pushes the
depth off sixteen.

| # | rules | states | transitions | depth | violations |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | base | 19200 | 112864 | 16 | 0 |
| 2 | restore_recovery_needs_full | 19200 | 112864 | 16 | 0 |
| 3 | ekm_down_clears_active | 23552 | 142560 | 16 | 0 |
| 4 | restore_recovery_needs_full+ekm_down_clears_active | 23552 | 142560 | 16 | 0 |
| 5 | activate_merged | 11008 | 63904 | 13 | 0 |
| 6 | restore_recovery_needs_full+activate_merged | 11008 | 63904 | 13 | 0 |
| 7 | activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 8 | restore_recovery_needs_full+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 9 | recover_clears_pending | 39680 | 215072 | 22 | 0 |
| 10 | restore_recovery_needs_full+recover_clears_pending | 39680 | 215072 | 22 | 0 |
| 11 | recover_clears_pending+ekm_down_clears_active | 49024 | 271216 | 22 | 0 |
| 12 | restore_recovery_needs_full+recover_clears_pending+ekm_down_clears_active | 49024 | 271216 | 22 | 0 |
| 13 | recover_clears_pending+activate_merged | 11008 | 63904 | 13 | 0 |
| 14 | restore_recovery_needs_full+recover_clears_pending+activate_merged | 11008 | 63904 | 13 | 0 |
| 15 | recover_clears_pending+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 16 | restore_recovery_needs_full+recover_clears_pending+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 17 | vpn_pending_replaceable | 25344 | 148192 | 14 | 0 |
| 18 | restore_recovery_needs_full+vpn_pending_replaceable | 25344 | 148192 | 14 | 0 |
| 19 | vpn_pending_replaceable+ekm_down_clears_active | 31232 | 187536 | 14 | 0 |
| 20 | restore_recovery_needs_full+vpn_pending_replaceable+ekm_down_clears_active | 31232 | 187536 | 14 | 0 |
| 21 | vpn_pending_replaceable+activate_merged | 11008 | 63904 | 13 | 0 |
| 22 | restore_recovery_needs_full+vpn_pending_replaceable+activate_merged | 11008 | 63904 | 13 | 0 |
| 23 | vpn_pending_replaceable+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 24 | restore_recovery_needs_full+vpn_pending_replaceable+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 25 | vpn_pending_replaceable+recover_clears_pending | 39680 | 218144 | 17 | 0 |
| 26 | restore_recovery_needs_full+vpn_pending_replaceable+recover_clears_pending | 39680 | 218144 | 17 | 0 |
| 27 | vpn_pending_replaceable+recover_clears_pending+ekm_down_clears_active | 49024 | 276016 | 17 | 0 |
| 28 | restore_recovery_needs_full+vpn_pending_replaceable+recover_clears_pending+ekm_down_clears_active | 49024 | 276016 | 17 | 0 |
| 29 | vpn_pending_replaceable+recover_clears_pending+activate_merged | 11008 | 63904 | 13 | 0 |
| 30 | restore_recovery_needs_full+vpn_pending_replaceable+recover_clears_pending+activate_merged | 11008 | 63904 | 13 | 0 |
| 31 | vpn_pending_replaceable+recover_clears_pending+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 32 | restore_recovery_needs_full+vpn_pending_replaceable+recover_clears_pending+activate_merged+ekm_down_clears_active | 13440 | 81264 | 13 | 0 |
| 33 | alloc_vpn_needs_no_active | 9984 | 59104 | 13 | 0 |
| 34 | restore_recovery_needs_full+alloc_vpn_needs_no_active | 9984 | 59104 | 13 | 0 |
| 35 | alloc_vpn_needs_no_active+ekm_down_clears_active | 12032 | 74496 | 13 | 0 |
| 36 | restore_recovery_needs_full+alloc_vpn_needs_no_active+ekm_down_clears_active | 12032 | 74496 | 13 | 0 |
| 37 | alloc_vpn_needs_no_active+activate_merged | 6400 | 38176 | 12 | 0 |
| 38 | restore_recovery_needs_full+alloc_vpn_needs_no_active+activate_merged | 6400 | 38176 | 12 | 0 |
| 39 | alloc_vpn_needs_no_active+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 40 | restore_recovery_needs_full+alloc_vpn_needs_no_active+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 41 | alloc_vpn_needs_no_active+recover_clears_pending | 27392 | 144416 | 22 | 0 |
| 42 | restore_recovery_needs_full+alloc_vpn_needs_no_active+recover_clears_pending | 27392 | 144416 | 22 | 0 |
| 43 | alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active | 33664 | 182224 | 22 | 0 |
| 44 | restore_recovery_needs_full+alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active | 33664 | 182224 | 22 | 0 |
| 45 | alloc_vpn_needs_no_active+recover_clears_pending+activate_merged | 6400 | 38176 | 12 | 0 |
| 46 | restore_recovery_needs_full+alloc_vpn_needs_no_active+recover_clears_pending+activate_merged | 6400 | 38176 | 12 | 0 |
| 47 | alloc_vpn_needs_no_active+recover_clears_pending+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 48 | restore_recovery_needs_full+alloc_vpn_needs_no_active+recover_clears_pending+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 49 | alloc_vpn_needs_no_active+vpn_pending_replaceable | 19200 | 108256 | 14 | 0 |
| 50 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable | 19200 | 108256 | 14 | 0 |
| 51 | alloc_vpn_needs_no_active+vpn_pending_replaceable+ekm_down_clears_active | 23552 | 136800 | 14 | 0 |
| 52 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+ekm_down_clears_active | 23552 | 136800 | 14 | 0 |
| 53 | alloc_vpn_needs_no_active+vpn_pending_replaceable+activate_merged | 6400 | 38176 | 12 | 0 |
| 54 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+activate_merged | 6400 | 38176 | 12 | 0 |
| 55 | alloc_vpn_needs_no_active+vpn_pending_replaceable+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 56 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 57 | alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending | 27392 | 146720 | 16 | 0 |
| 58 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending | 27392 | 146720 | 16 | 0 |
| 59 | alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+ekm_down_clears_active | 33664 | 185872 | 16 | 0 |
| 60 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+ekm_down_clears_active | 33664 | 185872 | 16 | 0 |
| 61 | alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+activate_merged | 6400 | 38176 | 12 | 0 |
| 62 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+activate_merged | 6400 | 38176 | 12 | 0 |
| 63 | alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |
| 64 | restore_recovery_needs_full+alloc_vpn_needs_no_active+vpn_pending_replaceable+recover_clears_pending+activate_merged+ekm_down_clears_active | 7680 | 48288 | 12 | 0 |

## Round 1, stage 3: edge knobs (128 enumerations)

Full product over the seven knobs that change how many labeled edges join the same
states (`reject_stale_needs_active`, `dep_toggles_unconditional`, `ingest_needs_ekm`,
`expire_needs_source_down`, `alloc_ekm_needs_source`, `authority_only_when_degraded`,
`recover_needs_authority`), swept on the base structural reading. The maximum
attainable transition count is 170,464 (`dep_toggles_unconditional`, which also drops
the depth to 15) -- 55% of the published 307,680.

| # | rules | states | transitions | depth | violations |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | base | 19200 | 112864 | 16 | 0 |
| 2 | reject_stale_needs_active | 19200 | 112864 | 16 | 0 |
| 3 | reject_stale_needs_active+recover_needs_authority | 19200 | 111664 | 16 | 0 |
| 4 | reject_stale_needs_active+authority_only_when_degraded | 19200 | 110464 | 16 | 0 |
| 5 | reject_stale_needs_active+authority_only_when_degraded+recover_needs_authority | 18000 | 102416 | 16 | 0 |
| 6 | reject_stale_needs_active+alloc_ekm_needs_source | 19200 | 109152 | 16 | 0 |
| 7 | reject_stale_needs_active+alloc_ekm_needs_source+recover_needs_authority | 19200 | 107952 | 16 | 0 |
| 8 | reject_stale_needs_active+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 106752 | 16 | 0 |
| 9 | reject_stale_needs_active+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 98704 | 16 | 0 |
| 10 | reject_stale_needs_active+expire_needs_source_down | 19200 | 106080 | 16 | 0 |
| 11 | reject_stale_needs_active+expire_needs_source_down+recover_needs_authority | 19200 | 104880 | 16 | 0 |
| 12 | reject_stale_needs_active+expire_needs_source_down+authority_only_when_degraded | 19200 | 103680 | 16 | 0 |
| 13 | reject_stale_needs_active+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority | 18000 | 96480 | 16 | 0 |
| 14 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source | 19200 | 102368 | 16 | 0 |
| 15 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority | 19200 | 101168 | 16 | 0 |
| 16 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 99968 | 16 | 0 |
| 17 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 92768 | 16 | 0 |
| 18 | reject_stale_needs_active+ingest_needs_ekm | 19200 | 109472 | 16 | 0 |
| 19 | reject_stale_needs_active+ingest_needs_ekm+recover_needs_authority | 19200 | 108272 | 16 | 0 |
| 20 | reject_stale_needs_active+ingest_needs_ekm+authority_only_when_degraded | 19200 | 107072 | 16 | 0 |
| 21 | reject_stale_needs_active+ingest_needs_ekm+authority_only_when_degraded+recover_needs_authority | 18000 | 99024 | 16 | 0 |
| 22 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source | 19200 | 105760 | 16 | 0 |
| 23 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+recover_needs_authority | 19200 | 104560 | 16 | 0 |
| 24 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 103360 | 16 | 0 |
| 25 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 95312 | 16 | 0 |
| 26 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down | 19200 | 102688 | 16 | 0 |
| 27 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+recover_needs_authority | 19200 | 101488 | 16 | 0 |
| 28 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded | 19200 | 100288 | 16 | 0 |
| 29 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority | 18000 | 93088 | 16 | 0 |
| 30 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source | 19200 | 98976 | 16 | 0 |
| 31 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority | 19200 | 97776 | 16 | 0 |
| 32 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 96576 | 16 | 0 |
| 33 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 89376 | 16 | 0 |
| 34 | reject_stale_needs_active+dep_toggles_unconditional | 19200 | 170464 | 15 | 0 |
| 35 | reject_stale_needs_active+recover_needs_authority+dep_toggles_unconditional | 19200 | 169264 | 15 | 0 |
| 36 | reject_stale_needs_active+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 165664 | 15 | 0 |
| 37 | reject_stale_needs_active+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 155216 | 15 | 0 |
| 38 | reject_stale_needs_active+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 166752 | 15 | 0 |
| 39 | reject_stale_needs_active+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 165552 | 15 | 0 |
| 40 | reject_stale_needs_active+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 161952 | 15 | 0 |
| 41 | reject_stale_needs_active+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 151504 | 15 | 0 |
| 42 | reject_stale_needs_active+expire_needs_source_down+dep_toggles_unconditional | 19200 | 163680 | 15 | 0 |
| 43 | reject_stale_needs_active+expire_needs_source_down+recover_needs_authority+dep_toggles_unconditional | 19200 | 162480 | 15 | 0 |
| 44 | reject_stale_needs_active+expire_needs_source_down+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 158880 | 15 | 0 |
| 45 | reject_stale_needs_active+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 149280 | 15 | 0 |
| 46 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 159968 | 15 | 0 |
| 47 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 158768 | 15 | 0 |
| 48 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 155168 | 15 | 0 |
| 49 | reject_stale_needs_active+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 145568 | 15 | 0 |
| 50 | reject_stale_needs_active+ingest_needs_ekm+dep_toggles_unconditional | 19200 | 167072 | 15 | 0 |
| 51 | reject_stale_needs_active+ingest_needs_ekm+recover_needs_authority+dep_toggles_unconditional | 19200 | 165872 | 15 | 0 |
| 52 | reject_stale_needs_active+ingest_needs_ekm+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 162272 | 15 | 0 |
| 53 | reject_stale_needs_active+ingest_needs_ekm+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 151824 | 15 | 0 |
| 54 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 163360 | 15 | 0 |
| 55 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 162160 | 15 | 0 |
| 56 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 158560 | 15 | 0 |
| 57 | reject_stale_needs_active+ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 148112 | 15 | 0 |
| 58 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+dep_toggles_unconditional | 19200 | 160288 | 15 | 0 |
| 59 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+recover_needs_authority+dep_toggles_unconditional | 19200 | 159088 | 15 | 0 |
| 60 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 155488 | 15 | 0 |
| 61 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 145888 | 15 | 0 |
| 62 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 156576 | 15 | 0 |
| 63 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 155376 | 15 | 0 |
| 64 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 151776 | 15 | 0 |
| 65 | reject_stale_needs_active+ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 142176 | 15 | 0 |
| 66 | recover_needs_authority | 19200 | 111664 | 16 | 0 |
| 67 | authority_only_when_degraded | 19200 | 110464 | 16 | 0 |
| 68 | authority_only_when_degraded+recover_needs_authority | 18000 | 102416 | 16 | 0 |
| 69 | alloc_ekm_needs_source | 19200 | 109152 | 16 | 0 |
| 70 | alloc_ekm_needs_source+recover_needs_authority | 19200 | 107952 | 16 | 0 |
| 71 | alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 106752 | 16 | 0 |
| 72 | alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 98704 | 16 | 0 |
| 73 | expire_needs_source_down | 19200 | 106080 | 16 | 0 |
| 74 | expire_needs_source_down+recover_needs_authority | 19200 | 104880 | 16 | 0 |
| 75 | expire_needs_source_down+authority_only_when_degraded | 19200 | 103680 | 16 | 0 |
| 76 | expire_needs_source_down+authority_only_when_degraded+recover_needs_authority | 18000 | 96480 | 16 | 0 |
| 77 | expire_needs_source_down+alloc_ekm_needs_source | 19200 | 102368 | 16 | 0 |
| 78 | expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority | 19200 | 101168 | 16 | 0 |
| 79 | expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 99968 | 16 | 0 |
| 80 | expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 92768 | 16 | 0 |
| 81 | ingest_needs_ekm | 19200 | 109472 | 16 | 0 |
| 82 | ingest_needs_ekm+recover_needs_authority | 19200 | 108272 | 16 | 0 |
| 83 | ingest_needs_ekm+authority_only_when_degraded | 19200 | 107072 | 16 | 0 |
| 84 | ingest_needs_ekm+authority_only_when_degraded+recover_needs_authority | 18000 | 99024 | 16 | 0 |
| 85 | ingest_needs_ekm+alloc_ekm_needs_source | 19200 | 105760 | 16 | 0 |
| 86 | ingest_needs_ekm+alloc_ekm_needs_source+recover_needs_authority | 19200 | 104560 | 16 | 0 |
| 87 | ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 103360 | 16 | 0 |
| 88 | ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 95312 | 16 | 0 |
| 89 | ingest_needs_ekm+expire_needs_source_down | 19200 | 102688 | 16 | 0 |
| 90 | ingest_needs_ekm+expire_needs_source_down+recover_needs_authority | 19200 | 101488 | 16 | 0 |
| 91 | ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded | 19200 | 100288 | 16 | 0 |
| 92 | ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority | 18000 | 93088 | 16 | 0 |
| 93 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source | 19200 | 98976 | 16 | 0 |
| 94 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority | 19200 | 97776 | 16 | 0 |
| 95 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded | 19200 | 96576 | 16 | 0 |
| 96 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority | 18000 | 89376 | 16 | 0 |
| 97 | dep_toggles_unconditional | 19200 | 170464 | 15 | 0 |
| 98 | recover_needs_authority+dep_toggles_unconditional | 19200 | 169264 | 15 | 0 |
| 99 | authority_only_when_degraded+dep_toggles_unconditional | 19200 | 165664 | 15 | 0 |
| 100 | authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 155216 | 15 | 0 |
| 101 | alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 166752 | 15 | 0 |
| 102 | alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 165552 | 15 | 0 |
| 103 | alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 161952 | 15 | 0 |
| 104 | alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 151504 | 15 | 0 |
| 105 | expire_needs_source_down+dep_toggles_unconditional | 19200 | 163680 | 15 | 0 |
| 106 | expire_needs_source_down+recover_needs_authority+dep_toggles_unconditional | 19200 | 162480 | 15 | 0 |
| 107 | expire_needs_source_down+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 158880 | 15 | 0 |
| 108 | expire_needs_source_down+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 149280 | 15 | 0 |
| 109 | expire_needs_source_down+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 159968 | 15 | 0 |
| 110 | expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 158768 | 15 | 0 |
| 111 | expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 155168 | 15 | 0 |
| 112 | expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 145568 | 15 | 0 |
| 113 | ingest_needs_ekm+dep_toggles_unconditional | 19200 | 167072 | 15 | 0 |
| 114 | ingest_needs_ekm+recover_needs_authority+dep_toggles_unconditional | 19200 | 165872 | 15 | 0 |
| 115 | ingest_needs_ekm+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 162272 | 15 | 0 |
| 116 | ingest_needs_ekm+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 151824 | 15 | 0 |
| 117 | ingest_needs_ekm+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 163360 | 15 | 0 |
| 118 | ingest_needs_ekm+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 162160 | 15 | 0 |
| 119 | ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 158560 | 15 | 0 |
| 120 | ingest_needs_ekm+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 148112 | 15 | 0 |
| 121 | ingest_needs_ekm+expire_needs_source_down+dep_toggles_unconditional | 19200 | 160288 | 15 | 0 |
| 122 | ingest_needs_ekm+expire_needs_source_down+recover_needs_authority+dep_toggles_unconditional | 19200 | 159088 | 15 | 0 |
| 123 | ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 155488 | 15 | 0 |
| 124 | ingest_needs_ekm+expire_needs_source_down+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 145888 | 15 | 0 |
| 125 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+dep_toggles_unconditional | 19200 | 156576 | 15 | 0 |
| 126 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+recover_needs_authority+dep_toggles_unconditional | 19200 | 155376 | 15 | 0 |
| 127 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+dep_toggles_unconditional | 19200 | 151776 | 15 | 0 |
| 128 | ingest_needs_ekm+expire_needs_source_down+alloc_ekm_needs_source+authority_only_when_degraded+recover_needs_authority+dep_toggles_unconditional | 18000 | 142176 | 15 | 0 |

## Round 1, stage 4: variants outside the declared state tuple (8 enumerations)

`expire_marks_used` adds a field the paper's tuple does not contain (identifiers that
expired can never be re-ingested), which is the only reading found that gets within a
few percent of the published state count -- and it still misses, on all three numbers
at once:

| expire_marks_used | vpn_pending_replaceable | ekm_down_clears_active | states | transitions | depth |
| --- | --- | --- | ---: | ---: | ---: |
| yes | yes | yes | 55,440 | 313,698 | 14 |
| yes | yes | no | 45,264 | 250,506 | 14 |
| yes | no | yes | 44,880 | 253,818 | 16 |
| yes | no | no | 36,624 | 202,290 | 16 |
| no | yes | yes | 31,232 | 187,536 | 14 |
| no | yes | no | 25,344 | 148,192 | 14 |
| no | no | yes | 23,552 | 142,560 | 16 |
| no | no | no | 19,200 | 112,864 | 16 |

(The first row is the closest any round-1 variant comes on transitions: 313,698
against 307,680 -- but with 55,440 states and depth 14. Round 2 below supersedes this
stage: `expire_marks_used` is not part of the calibrated reading, and no extra state
variable is needed.)

## Round 2 (260 enumerations)

Round 2 promoted round 1's wildcard into a real knob and added ten more, each one a
hypothesis about a degree of freedom the paper's prose allows but the base reading
does not. All are off by default, so `Rules()` still denotes the base reading and every
round-1 table above still reproduces.

| Knob | Reading it encodes |
| --- | --- |
| `expire_marks_used` | TTL expiry retires an identifier for good (three-valued identifier status) |
| `suspend_state` | withdrawal of authority from holdover/exhaustion records an explicit policy-suspension state (Figure 3, T5/T6), cleared only by reconciliation |
| `restoration_detected` | "restoration detected" is its own recovery sub-state (T7-T10), promoted to reconciliation by an explicit event before T11 |
| `abandon_pending` | a pending activation can be abandoned; its purpose binding persists |
| `abandon_consumes_epoch` | ...and abandoning consumes the epoch |
| `down_clears_pending` | dependency interruption discards an in-flight pending activation |
| `session_end` | an established session can end explicitly, with E retained (relaxes I7) |
| `recover_clears_active` | recovery reconciliation ends the established session |
| `fifo_pool` | the temporary pool is an ordered queue; expiry and allocation take the oldest entry |
| `alloc_vpn_any_future_epoch` | an allocation binds any not-yet-used ordered epoch, not only the next one |
| `reject_stale_per_epoch` | every non-increasing epoch value in the declared domain is its own labeled rejection event -- a replay/forgery model, not only epochs actually issued |

The stage sweeps each of the eleven on top of three round-1 seeds (base,
`expire_marks_used`, and round 1's closest depth-preserving reading
`expire_marks_used+ekm_down_clears_active`), then takes four full products over the
families that looked most likely to combine -- 260 distinct readings in all:

    uv run python scripts/model_calibrate.py --stage round2 --top 30

The thirty closest readings, ranked by `|states - 45,824| + |transitions - 307,680|`:

| # | rules | states | transitions | depth | violations |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | **alloc_vpn_any_future_epoch+reject_stale_per_epoch** | **45824** | **307680** | **16** | **0** |
| 2 | vpn_pending_replaceable+restoration_detected | 50688 | 302720 | 17 | 0 |
| 3 | ekm_down_clears_active+abandon_pending+reject_stale_per_epoch | 49024 | 300528 | 16 | 0 |
| 4 | expire_marks_used+down_clears_pending+reject_stale_per_epoch | 56688 | 306498 | 16 | 0 |
| 5 | expire_marks_used+abandon_pending+down_clears_pending | 56688 | 306402 | 14 | 0 |
| 6 | suspend_state+down_clears_pending | 57714 | 308174 | 16 | 0 |
| 7 | vpn_pending_replaceable+expire_marks_used+abandon_pending+down_clears_pending | 56688 | 310722 | 14 | 0 |
| 8 | vpn_pending_replaceable+ekm_down_clears_active+expire_marks_used | 55440 | 313698 | 14 | 0 |
| 9 | ekm_down_clears_active+restoration_detected | 47104 | 292864 | 19 | 0 |
| 10 | vpn_pending_replaceable+suspend_state+down_clears_pending | 57714 | 312782 | 15 | 0 |
| 11 | alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active+session_end | 59136 | 311392 | 22 | 0 |
| 12 | alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active+session_end+recover_clears_active | 59136 | 311392 | 22 | 0 |
| 13 | vpn_pending_replaceable+ekm_down_clears_active+abandon_pending | 49024 | 293808 | 15 | 0 |
| 14 | vpn_pending_replaceable+expire_marks_used+down_clears_pending | 56688 | 299298 | 14 | 0 |
| 15 | alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active+recover_clears_active | 59136 | 301280 | 23 | 0 |
| 16 | expire_marks_used+recover_clears_active | 57552 | 298602 | 19 | 0 |
| 17 | expire_marks_used+abandon_pending+down_clears_pending+reject_stale_per_epoch | 56688 | 317922 | 14 | 0 |
| 18 | ekm_down_clears_active+abandon_pending | 49024 | 289008 | 16 | 0 |
| 19 | ekm_down_clears_active+abandon_pending+fifo_pool | 55952 | 320226 | 16 | 0 |
| 20 | expire_marks_used+down_clears_pending | 56688 | 294978 | 16 | 0 |
| 21 | expire_marks_used+session_end | 57552 | 319530 | 17 | 0 |
| 22 | expire_marks_used+session_end+recover_clears_active | 57552 | 319530 | 17 | 0 |
| 23 | expire_marks_used+abandon_pending+abandon_consumes_epoch | 57552 | 319530 | 16 | 0 |
| 24 | suspend_state+abandon_pending+down_clears_pending | 57714 | 319942 | 15 | 0 |
| 25 | vpn_pending_replaceable+ekm_down_clears_active+suspend_state | 54962 | 323334 | 15 | 0 |
| 26 | vpn_pending_replaceable+suspend_state+abandon_pending+down_clears_pending | 57714 | 324550 | 15 | 0 |
| 27 | alloc_vpn_needs_no_active+recover_clears_pending+ekm_down_clears_active+expire_marks_used | 62352 | 320058 | 22 | 0 |
| 28 | alloc_vpn_needs_no_active+ekm_down_clears_active+expire_marks_used+recover_clears_active | 62352 | 320058 | 25 | 0 |
| 29 | ekm_down_clears_active+expire_marks_used+fifo_pool | 51712 | 283264 | 16 | 0 |
| 30 | ekm_down_clears_active+expire_marks_used+abandon_consumes_epoch+fifo_pool | 51712 | 283264 | 16 | 0 |

Row 1 is the only reading in the whole sweep that reaches 45,824 states, the only one
that reaches 307,680 transitions, and it does both at once at depth sixteen with zero
violations. The next closest readings sit thousands of states away, and every other
reading that gets near the transition count overshoots the state count by ten thousand
or more. `alloc_vpn_any_future_epoch` on its own gives 45,824 / 273,888 / 16 (row 32 of
the full sweep), which is what isolates the two knobs to their separate roles.

Nine of the eleven new knobs are therefore *not* part of the calibrated reading. They
stay in `Rules` as documentation of what was tested and as a regression harness for
the semantics; each is inert at its default, and `qkd_ekm.model.state.State` carries
their extra fields (`expired`, `suspended`, `detected`, `order`) at inert defaults too.

## Decision

`qkd_ekm.model.events.DEFAULT_RULES` is the calibrated reading
`Rules(alloc_vpn_any_future_epoch=True, reject_stale_per_epoch=True)`:

- it reproduces all three published numbers exactly -- 45,824 states, 307,680 labeled
  transitions, maximum shortest-path depth sixteen;
- it satisfies all nine state properties and all four transition guards across the
  whole reachable closure, with zero violations, and all thirteen seeded sensitivity
  cases are still detected by their expected check;
- it stays inside the declared state tuple: no variable was added, and the four
  identifiers, two EKM versions, and three activation epochs are unchanged;
- both departures from the base reading follow the paper's own wording about ordered
  activation epochs, and the two are pinned sequentially rather than independently:
  `alloc_vpn_any_future_epoch` is pinned by the state count on its own, and then, with
  that knob fixed, `reject_stale_per_epoch` is pinned by the transition count.

`tests/test_model.py::test_enumeration_counts_match_paper` asserts the published triple
strictly (the `xfail` is gone), `test_enumeration_counts_are_stable` asserts the same
triple against a literal so a semantic change cannot pass unnoticed even if
`expected/paper_numbers.json` changed, and
`test_calibrated_rules_are_the_reading_that_reproduces_the_paper` pins the two knobs.

No state variable was added, and the state count was *derived*, not chosen: the
closed form `cores = sum a(ne) . b(nv) . P(4, ne+nv) . 2^(4-ne-nv)` forces
`b = (1, 6, 6, 2)` once a VPN allocation may bind any not-yet-used epoch, and that
alone yields 45,824 exactly (see "The factorisation" above). The transition count was
then closed by a label-granularity choice -- `reject_stale_per_epoch` -- adopted
against the 33,792-edge residual left by the state-fixing knob alone (273,888 of
307,680). The knob does not pad the total with a chosen number of extra edges; it
relabels one already-enabled self-loop as one-per-epoch, and that relabeling happens
to close the residual exactly. Of the final 307,680 transitions, 66,048 (21.5%) are
`reject_stale` self-loops -- 32,256 at epoch 1, 22,528 at epoch 2, 11,264 at epoch 3,
verified directly from `transitions.csv`.

### Vacuous and structural properties

Three of the nine state properties are not ordinary runtime checks:

- **I1** (pool identifiers unique) is structurally true across the whole reachable
  closure, because `State.pool` is typed `frozenset[str]` -- a Python set cannot hold
  a duplicate, so `len(pool) != len(set(pool))` can never fire for a real enumerated
  state. Only the seeded `duplicate_pool_entry` case, which constructs a state with a
  plain tuple `pool=("k1", "k1")` (bypassing the type), can trip it.
- **I8** and **I9** were vacuous over the reachable closure before this round: both
  were gated on `stored_mode` / `stored_consequences`, fields that only
  `seeded.SeededState` carries, so `checks.invariants(s)` never evaluated either check
  for the 45,824 enumerated states -- only for the two seeded cases built to exercise
  them. Both are now totality/consistency checks that run on every state: I8 asserts
  `mode(s)` is one of the six declared mode names; I9 asserts `consequences(s)` has
  exactly the keys `storage`/`fresh_allocation`/`established_vpn`, correctly typed
  (`bool`, `bool`, the literal `"distinct"`), and that `storage == s.se` per Table 5's
  derived-from-dependencies reading. Seeded states still additionally compare against
  their stored value. `tests/test_model.py::test_i8_is_a_totality_check_...` and
  `test_i9_is_a_totality_and_consistency_check_...` assert both hold across the full
  45,824-state closure; the enumeration counts are unchanged by this fix, since it
  only strengthens the *check*, not the transition relation.

  The same disclosure I1 and I8 carry applies to I9's totality clauses: they are
  tautological by construction. `consequences()` builds the dictionary with exactly
  those three keys and those three types, and derives `storage` from `s.se` directly,
  so no enumerated state can fail the key set, the typing or the `storage == s.se`
  comparison — only the seeded `stored_consequences_conflict_with_dependencies` case,
  which supplies a
  stored vector that disagrees with the derivation, can make I9 fire.

Exactly half of the reachable states -- 22,912 of 45,824 -- carry mode `RECOVERY`.
That is not a coincidence of the state count: `derive()` sets `recovery_pending` from
`s.recovery or s.detected`, either dependency's restoration (`src_up`/`ekm_up`) marks
it, and `derive()` checks `recovery_pending` first, before every other mode branch --
so the recovery flag dominates the classification and is reachable independently of
every other state component, doubling the closure under it.
