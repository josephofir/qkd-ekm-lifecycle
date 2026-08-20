# Lifecycle reference model report

- Reachable states: 45824
- Labeled transitions: 307680
- Maximum shortest-path depth: 16
- State properties checked: I1, I2, I3, I4, I5, I6, I7, I8, I9
- Transition guards checked: G1, G2, G3, G4
- Violations: 0

## Reachable states by lifecycle mode

| Mode | States |
| --- | ---: |
| BINDING_HOLDOVER | 6772 |
| BUFFERED | 3166 |
| EXHAUSTED | 237 |
| READY | 5728 |
| RECOVERY | 22912 |
| SUSPENDED | 7009 |

## Seeded sensitivity analysis

- Seeded cases: 13
- Undetected: 0

| Case | Kind | Expected | Detected by |
| --- | --- | --- | --- |
| duplicate_pool_entry | state | I1 | I1 |
| identifier_in_two_bindings | state | I2 | I2 |
| consumed_identifier_back_in_pool | state | I3 | I3 |
| two_identifiers_same_purpose_object | state | I4 | I4 |
| active_version_without_binding | state | I5 | I5 |
| pending_activation_with_replayed_epoch | state | I6 | I6 |
| active_vpn_epoch_conflicts_with_authoritative_epoch | state | I7 | I7 |
| stored_mode_conflicts_with_derived_mode | state | I8 | I8 |
| stored_consequences_conflict_with_dependencies | state | I9 | I9 |
| ingest_while_source_unavailable | transition | G1 | G1 |
| alloc_ekm_while_ekm_unreachable | transition | G2 | G2 |
| reject_stale_changes_authoritative_state | transition | G3 | G3 |
| recover_without_restored_dependencies | transition | G4 | G4 |
