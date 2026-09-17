# Slice3 batch integrity check

Complete cells: 9/9  
Case rows: 90/90  
Proof-level verification disagreements: 1  
Identity fields stable: yes  

| case | narrow == skill | control distinct | control schema | narrow/skill schema |
|---|---|---|---|---|
| clean-build | yes | yes | `969c3262b19358dd` | `829af1b652023968` |
| compile-error-fix | yes | yes | `969c3262b19358dd` | `e500b006aa41dea9` |
| compile-error-locate | yes | yes | `969c3262b19358dd` | `c21935ce6e5cd069` |
| link-error | yes | yes | `969c3262b19358dd` | `c21935ce6e5cd069` |
| navigation | yes | yes | `969c3262b19358dd` | `7fb3102bc960cea0` |
| review-restraint | yes | yes | `969c3262b19358dd` | `4f2aa82886ac8066` |
| segfault | yes | yes | `969c3262b19358dd` | `fccf4152d038444d` |
| test-failure-diagnose | yes | yes | `969c3262b19358dd` | `fccf4152d038444d` |
| test-failure-fix | yes | yes | `969c3262b19358dd` | `e951aaa1c08f75a1` |
| timeout | yes | yes | `969c3262b19358dd` | `fccf4152d038444d` |

## Verdict

**FAIL**

- proof-level verification disagreements: 1
