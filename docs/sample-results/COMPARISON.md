# Comparison against the published numbers

43 of 43 checks pass.

| check | expected | actual | result | note |
| --- | ---: | ---: | --- | --- |
| model.states | 45824 | 45824 | PASS |  |
| model.transitions | 307680 | 307680 | PASS |  |
| model.max_depth | 16 | 16 | PASS |  |
| model.invariants | 9 | 9 | PASS |  |
| model.guards | 4 | 4 | PASS |  |
| model.seeded_states_detected | 9 | 9 | PASS |  |
| model.seeded_transitions_detected | 4 | 4 | PASS |  |
| table9[1].demand | 0.001389 | 0.00138889 | PASS |  |
| table9[1].headroom | 59681.2 | 59681.2 | PASS |  |
| table9[1].endurance_h | 655194 | 655194 | PASS |  |
| table9[1].refill_min | 0.001 | 0.00100536 | PASS |  |
| table9[1000].demand | 1.38889 | 1.38889 | PASS |  |
| table9[1000].headroom | 59.681 | 59.6812 | PASS |  |
| table9[1000].endurance_h | 655.194 | 655.194 | PASS |  |
| table9[1000].refill_min | 1.022 | 1.02247 | PASS |  |
| table9[10000].demand | 13.8889 | 13.8889 | PASS |  |
| table9[10000].headroom | 5.968 | 5.96812 | PASS |  |
| table9[10000].endurance_h | 65.519 | 65.5194 | PASS |  |
| table9[10000].refill_min | 12.078 | 12.077 | PASS |  |
| table9[50000].demand | 69.4444 | 69.4444 | PASS |  |
| table9[50000].headroom | 1.194 | 1.19362 | PASS |  |
| table9[50000].endurance_h | 13.104 | 13.1039 | PASS |  |
| table9[50000].refill_min | 309.88 | 309.877 | PASS |  |
| table9[60000].demand | 83.3333 | 83.3333 | PASS |  |
| table9[60000].headroom | 0.995 | 0.994687 | PASS |  |
| table9[60000].endurance_h | 10.92 | 10.9199 | PASS |  |
| table9[60000].refill_min | null | null | PASS |  |
| scalars.D | 0.00138889 | 0.00138889 | PASS |  |
| scalars.mQ | 59681.2 | 59681.2 | PASS |  |
| scalars.reserve_24h_1000_pct | 96.3 | 96.337 | PASS |  |
| scalars.reserve_24h_10000_pct | 63.4 | 63.3696 | PASS |  |
| scalars.depletion_50000_h | 13.1 | 13.1039 | PASS |  |
| scalars.refill_1h_1000_min | 1.02 | 1.02247 | PASS |  |
| scalars.refill_1h_10000_min | 12.1 | 12.077 | PASS |  |
| scalars.refill_1h_50000_h | 5.16 | 5.16462 | PASS |  |
| s1.VPNClient | 8 events in order | 8 of 8 matched | PASS |  |
| s1.VPNServer | 5 events in order | 5 of 5 matched | PASS |  |
| s1.EKM | 2 events in order | 2 of 2 matched | PASS |  |
| s1.sequence | 15 events in order | 15 of 15 matched | PASS |  |
| s2.EKM | 3 events in order | 3 of 3 matched | PASS |  |
| s2.FileUploadServer | 5 events in order | 5 of 5 matched | PASS |  |
| s2.VPNClient | 4 events in order | 4 of 4 matched | PASS |  |
| s2.sequence | 12 events in order | 12 of 12 matched | PASS |  |
