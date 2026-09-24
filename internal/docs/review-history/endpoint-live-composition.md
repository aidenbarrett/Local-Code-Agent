# Endpoint live composition review note

Base: `ffd21a3a0b9857edc4fae053fb7674d497966fa1` (`main` when this slice started).

Scope: public Session Hub conversation inference plus admitted worker inference only. Both use the existing in-process endpoint lease stack. No new scheduler, no cross-process claim, no frozen experiment changes.

Required evidence before merge: Linux/Windows authoritative tests and product acceptance on the exact PR head. Physical Panther Lake/NPU acceptance remains separate and is not implied by CI.
