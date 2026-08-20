"""Executable lifecycle reference model (paper section 5).

The model is a finite abstraction of the external-key lifecycle: four key
identifiers, two EKM versions, three VPN activation epochs, source/EKM
availability, continuity authority, and recovery reconciliation. It is
enumerated exhaustively (breadth-first) so that the nine state properties
(I1-I9) and four transition guards (G1-G4) are checked on every reachable
state and every labeled transition.
"""
